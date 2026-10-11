import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta
import sqlite3
import os

# --- ตั้งค่าคอนฟิกหลัก ---
TARGET_GUILD_ID = 1555496169029767218
TARGET_GUILD = discord.Object(id=TARGET_GUILD_ID)
ADMIN_USER_ID = 933529869487321161  # ID ของแอดมินผู้ดูแลระบบ
DB_FILE = "bot_database.db"

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True  # จำเป็นสำหรับการอ่านข้อความใน DM หรือห้องซัพพอร์ต

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# voice_sessions: เก็บเวลาเข้าห้อง Voice ปัจจุบัน {user_id: datetime}
voice_sessions = {}

# --- ระบบฐานข้อมูล SQLite ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # ตารางเก็บบันทึกเวลา Voice ย้อนหลัง 30 วัน
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS voice_time (
            user_id INTEGER,
            date_str TEXT,
            seconds REAL,
            PRIMARY KEY (user_id, date_str)
        )
    ''')
    # ตารางเก็บการตั้งค่าห้องคุยผ่านบอท (guild_id, channel_id)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS support_channels (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER
        )
    ''')
    # ตารางจับคู่บทสนทนาระหว่างสมาชิกกับแอดมิน (member_id, admin_id)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_sessions (
            member_id INTEGER PRIMARY KEY,
            admin_id INTEGER
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- ฟังก์ชันจัดการระบบ Voice Time ---
def add_voice_time(user_id, seconds):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    cursor.execute('''
        INSERT INTO voice_time (user_id, date_str, seconds) VALUES (?, ?, ?)
        ON CONFLICT(user_id, date_str) DO UPDATE SET seconds = seconds + ?
    ''', (user_id, today_str, seconds, seconds))
    
    # ลบข้อมูลที่เก่าเกิน 30 วัน
    cutoff_date = (datetime.now().date() - timedelta(days=30)).strftime("%Y-%m-%d")
    cursor.execute('DELETE FROM voice_time WHERE user_id = ? AND date_str < ?', (user_id, cutoff_date))
    
    conn.commit()
    conn.close()

def get_total_time_30days(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cutoff_date = (datetime.now().date() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    cursor.execute('SELECT SUM(seconds) FROM voice_time WHERE user_id = ? AND date_str >= ?', (user_id, cutoff_date))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result and result[0] else 0

# --- ฟังก์ชันจัดการระบบ Support Channel ---
def set_support_channel(guild_id, channel_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO support_channels (guild_id, channel_id) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET channel_id = ?
    ''', (guild_id, channel_id, channel_id))
    conn.commit()
    conn.close()

def get_support_channel(guild_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT channel_id FROM support_channels WHERE guild_id = ?', (guild_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

# --- บอทเริ่มต้นทำงาน ---
@bot.event
async def on_ready():
    print(f'บอทเชื่อมต่อสำเร็จในชื่อ: {bot.user}')
    try:
        tree.copy_global_to(guild=TARGET_GUILD)
        synced = await tree.sync(guild=TARGET_GUILD)
        print(f'ซิงก์ Slash Commands สำเร็จจำนวน {len(synced)} คำสั่ง')
    except Exception as e:
        print(f'เกิดข้อผิดพลาดในการซิงก์คำสั่ง: {e}')

# --- ระบบนับเวลา Voice Channel ---
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or member.guild.id != TARGET_GUILD_ID:
        return

    user_id = member.id

    if before.channel is None and after.channel is not None:
        voice_sessions[user_id] = datetime.now()
    elif before.channel is not None and after.channel is None:
        if user_id in voice_sessions:
            join_time = voice_sessions.pop(user_id)
            duration = (datetime.now() - join_time).total_seconds()
            add_voice_time(user_id, duration)

# --- ระบบส่งข้อความคุยผ่านบอท (Proxy Chat & DM Handling) ---
@bot.event
async def on_message(message):
    # ป้องกันไม่ให้บอทตอบข้อความตัวเองหรือบอทอื่นๆ
    if message.author.bot:
        return

    # กรณีที่ 1: สมาชิกพิมพ์ข้อความในช่องซัพพอร์ตของเซิร์ฟเวอร์
    if message.guild and message.guild.id == TARGET_GUILD_ID:
        support_channel_id = get_support_channel(TARGET_GUILD_ID)
        if support_channel_id and message.channel.id == support_channel_id:
            # ลบข้อความต้นฉบับของสมาชิกทันทีเพื่อความเป็นส่วนตัวและความสะอาด
            try:
                await message.delete()
            except Exception:
                pass

            member = message.author
            content = message.content

            # บันทึกหรืออัปเดตการจับคู่ว่า สมาชิกคนนี้คุยกับแอดมินคนนี้
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO chat_sessions (member_id, admin_id) VALUES (?, ?)
                ON CONFLICT(member_id) DO UPDATE SET admin_id = ?
            ''', (member.id, ADMIN_USER_ID, ADMIN_USER_ID))
            conn.commit()
            conn.close()

            # ส่งข้อความไปยัง DM ของแอดมิน โดยแสดงข้อมูลสมาชิกแบบปลอดภัย
            try:
                admin_user = await bot.fetch_user(ADMIN_USER_ID)
                if admin_user:
                    embed = discord.Embed(
                        title="💬 มีข้อความใหม่จากสมาชิก",
                        description=content,
                        color=discord.Color.green(),
                        timestamp=datetime.now()
                    )
                    embed.add_field(name="ข้อมูลผู้ส่ง", value=f"ชื่อ: {member.display_name}\nID: `{member.id}`", inline=False)
                    embed.set_footer(text="วิธีตอบกลับ: ให้พิมพ์ข้อความตอบกลับหาบอทโดยตรง หรือระบบจะส่งต่อให้สมาชิกคนนี้")
                    await admin_user.send(embed=embed)
                    
                    # แจ้งเตือนสั้นๆ ในห้องแชทให้สมาชิกทราบว่าบอทส่งเรื่องให้แล้ว (หายไปเองอัตโนมัติ)
                    temp_msg = await message.channel.send(f"✅ {member.mention} ส่งข้อความถึงผู้ดูแลเรียบร้อยแล้ว", delete_after=5)
            except Exception as e:
                print(f"Error sending DM to admin: {e}")
                await message.channel.send("⚠️ เกิดข้อผิดพลาดในการส่งข้อความถึงผู้ดูแล กรุณาลองใหม่อีกครั้ง", delete_after=5)

        return

    # กรณีที่ 2: ข้อความส่งมาทาง Direct Message (DM) หาบอท
    if message.guild is None:
        sender_id = message.author.id
        content = message.content

        # ถ้าผู้ส่งคือแอดมินเป้าหมาย ตอบกลับไปยังสมาชิก
        if sender_id == ADMIN_USER_ID:
            # ค้นหาว่าแอดมินกำลังคุยกับสมาชิกคนล่าสุดคนไหนอยู่จากฐานข้อมูล
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('SELECT member_id FROM chat_sessions WHERE admin_id = ? ORDER BY rowid DESC LIMIT 1', (ADMIN_USER_ID,))
            result = cursor.fetchone()
            conn.close()

            if result:
                target_member_id = result[0]
                try:
                    guild = bot.get_guild(TARGET_GUILD_ID)
                    target_member = guild.get_member(target_member_id)
                    support_channel_id = get_support_channel(TARGET_GUILD_ID)
                    support_channel = guild.get_channel(support_channel_id)

                    if support_channel and target_member:
                        # ส่งข้อความตอบกลับลงในห้องซัพพอร์ต โดยระบุว่ามาจากบอทเท่านั้น ห้ามเผยชื่อแอดมิน
                        embed = discord.Embed(
                            description=content,
                            color=discord.Color.gold(),
                            timestamp=datetime.now()
                        )
                        embed.set_author(name="ฝ่ายสนับสนุน (Support)", icon_url=bot.user.display_avatar.url)
                        await support_channel.send(content=f"{target_member.mention}", embed=embed)
                        await message.add_reaction("✅")
                    else:
                        await message.channel.send("⚠️ ไม่พบห้องซัพพอร์ตหรือสมาชิกคนดังกล่าวในเซิร์ฟเวอร์แล้ว")
                except Exception as e:
                    print(f"Error forwarding admin message: {e}")
                    await message.channel.send("⚠️ ส่งข้อความหาFAIL: เกิดข้อผิดพลาดในการส่งข้อมูล")
            else:
                await message.channel.send("⚠️ ยังไม่มีสมาชิกกำลังสนทนาด้วย หรือยังไม่มีการเลือกห้องเป้าหมาย")

        else:
            # กรณีสมาชิกทั่วไปส่ง DM หาบอทโดยตรง (แนะนำให้บอทแจ้งให้ไปคุยผ่านห้องในเซิร์ฟเวอร์แทน)
            try:
                await message.author.send("กรุณาส่งข้อความผ่านห้องซัพพอร์ตในเซิร์ฟเวอร์ที่กำหนดไว้ครับ")
            except Exception:
                pass

# --- Slash Command: /online (เช็คเวลา Voice) ---
@tree.command(name="online", description="เช็คเวลาออนไลน์ในห้องเสียง (ย้อนหลัง 30 วัน)", guild=TARGET_GUILD)
@app_commands.describe(member="เลือกสมาชิกที่ต้องการเช็คเวลา (ถ้าไม่เลือกจะเป็นการเช็คเวลาตนเอง)")
async def check_online(interaction: discord.Interaction, member: discord.Member = None):
    if interaction.guild_id != TARGET_GUILD_ID:
        await interaction.response.send_message("คำสั่งนี้อนุญาตให้ใช้เฉพาะในเซิร์ฟเวอร์ที่กำหนดเท่านั้น", ephemeral=True)
        return

    target_member = member or interaction.user
    user_id = target_member.id

    accumulated_seconds = get_total_time_30days(user_id)
    current_session_seconds = 0
    if user_id in voice_sessions:
        current_session_seconds = (datetime.now() - voice_sessions[user_id]).total_seconds()

    total_seconds = int(accumulated_seconds + current_session_seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    embed = discord.Embed(
        title=f"⏱️ เวลาออนไลน์ใน Voice ของ {target_member.display_name}",
        color=discord.Color.blue()
    )
    embed.add_field(name="เวลารวมสะสม (ย้อนหลัง 30 วัน)", value=f"{hours} ชั่วโมง {minutes} นาที {seconds} วินาที", inline=False)
    
    if user_id in voice_sessions:
        embed.set_footer(text="🟢 กำลังออนไลน์อยู่ในห้องเสียง ณ ขณะนี้")
    else:
        embed.set_footer(text="🔴 ไม่ได้อยู่ในห้องเสียง")

    await interaction.response.send_message(embed=embed)

# --- Slash Command: /รอแก้ (ตั้งค่าห้องคุยผ่านบอท - เฉพาะแอดมิน) ---
@tree.command(name="รอแก้", description="ตั้งค่าห้องสำหรับระบบคุยผ่านบอท (เฉพาะผู้ดูแล)", guild=TARGET_GUILD)
@app_commands.describe(channel="เลือกห้องแชทที่ต้องการเปิดระบบคุยผ่านบอท")
async def setup_support(interaction: discord.Interaction, channel: discord.TextChannel):
    # ตรวจสอบสิทธิ์เฉพาะ User ID ของแอดมินที่กำหนดเท่านั้น
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message("คุณไม่มีสิทธิ์ใช้งานคำสั่งนี้", ephemeral=True)
        return

    set_support_channel(interaction.guild_id, channel.id)
    
    # ตอบกลับแบบ Ephemeral ให้เห็นเฉพาะแอดมินคนเดียว
    await interaction.response.send_message("«✅ เปิดระบบคุยผ่านบอทเรียบร้อยแล้ว»", ephemeral=True)

# --- Slash Command: /ตามบอท (แจ้งเตือนแอดมิน) ---
@tree.command(name="ตามบอท", description="ส่งแจ้งเตือนเรียกผู้ดูแลระบบผ่านบอท", guild=TARGET_GUILD)
async def call_admin(interaction: discord.Interaction):
    if interaction.guild_id != TARGET_GUILD_ID:
        await interaction.response.send_message("คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์ที่กำหนดเท่านั้น", ephemeral=True)
        return

    member = interaction.user
    guild = interaction.guild
    channel = interaction.channel
    current_time = datetime.now().strftime("%H:%M")

    # ส่ง DM แจ้งเตือนไปยังแอดมินเป้าหมาย
    try:
        admin_user = await bot.fetch_user(ADMIN_USER_ID)
        if admin_user:
            embed = discord.Embed(
                title="🔔 มีสมาชิกเรียกบอท",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            embed.add_field(name="ผู้ใช้", value=member.display_name, inline=True)
            embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)
            embed.add_field(name="เซิร์ฟเวอร์", value=guild.name, inline=False)
            embed.add_field(name="ห้อง", value=channel.mention, inline=True)
            embed.add_field(name="เวลา", value=current_time, inline=True)
            
            await admin_user.send(embed=embed)
    except Exception as e:
        print(f"Error notifying admin: {e}")

    # ตอบกลับสมาชิกแบบเห็นเฉพาะตัว (Ephemeral) เพื่อความเป็นส่วนตัว
    await interaction.response.send_message("✅ ส่งคำขอเรียกว่าบอทไปยังผู้ดูแลเรียบร้อยแล้ว", ephemeral=True)

# รันบอทผ่าน Environment Variable (DISCORD_TOKEN) บน Railway
bot.run(os.getenv("DISCORD_TOKEN"))
