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
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# voice_sessions: เก็บเวลาเข้าห้อง Voice ปัจจุบัน {user_id: datetime}
voice_sessions = {}

# เก็บสถานะว่าขณะนี้แอดมินกำลังเลือกคุยกับ User ID ไหนอยู่ {admin_id: member_id}
admin_active_target = {}

# --- ระบบฐานข้อมูล SQLite ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS voice_time (
            user_id INTEGER,
            date_str TEXT,
            seconds REAL,
            PRIMARY KEY (user_id, date_str)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS support_channels (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_sessions (
            member_id INTEGER PRIMARY KEY,
            admin_id INTEGER,
            last_message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
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

# --- UI Component: เมนูดรอปดาวน์สำหรับเลือกสมาชิกที่จะตอบกลับใน DM ---
class TargetSelectView(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot = bot_instance
        self.update_options()

    def update_options(self):
        self.clear_items()
        
        # ดึงรายชื่อสมาชิกที่เคยทักมาล่าสุด 25 คนแรกจากฐานข้อมูล
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT member_id FROM chat_sessions ORDER BY timestamp DESC LIMIT 25')
        rows = cursor.fetchall()
        conn.close()

        options = []
        guild = self.bot.get_guild(TARGET_GUILD_ID)
        
        for row in rows:
            m_id = row[0]
            member_name = f"User ID: {m_id}"
            if guild:
                m_obj = guild.get_member(m_id)
                if m_obj:
                    member_name = m_obj.display_name
            
            options.append(discord.SelectOption(
                label=member_name[:100], 
                value=str(m_id), 
                description=f"ID: {m_id}"
            ))

        if not options:
            options.append(discord.SelectOption(label="ยังไม่มีสมาชิกส่งข้อความ", value="none"))

        select = discord.ui.Select(placeholder="📌 เลือกสมาชิกที่ต้องการตอบกลับ...", options=options, min_values=1, max_values=1)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID:
            await interaction.response.send_message("คุณไม่มีสิทธิ์ใช้งานเมนูนี้", ephemeral=True)
            return

        selected_id = int(interaction.data["values"][0])
        admin_active_target[interaction.user.id] = selected_id
        
        guild = self.bot.get_guild(TARGET_GUILD_ID)
        target_member = guild.get_member(selected_id) if guild else None
        target_name = target_member.display_name if target_member else str(selected_id)

        await interaction.response.send_message(f"✅ ตั้งค่าเป้าหมายปัจจุบันเป็น: **{target_name}** (`{selected_id}`)เรียบร้อยแล้ว พิมพ์ข้อความส่งมาในนี้ได้เลยครับ", ephemeral=True)

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
    if message.author.bot:
        return

    # กรณีที่ 1: สมาชิกพิมพ์ข้อความในห้องซัพพอร์ต (ข้อความไม่หายแล้ว)
    if message.guild and message.guild.id == TARGET_GUILD_ID:
        support_channel_id = get_support_channel(TARGET_GUILD_ID)
        if support_channel_id and message.channel.id == support_channel_id:
            member = message.author
            content = message.content

            # บันทึกเซสชันการคุย
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO chat_sessions (member_id, admin_id, last_message, timestamp) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(member_id) DO UPDATE SET admin_id = ?, last_message = ?, timestamp = CURRENT_TIMESTAMP
            ''', (member.id, ADMIN_USER_ID, content, ADMIN_USER_ID, content))
            conn.commit()
            conn.close()

            # ตั้งค่าให้แอดมินคุยกับคนล่าสุดอัตโนมัติ
            admin_active_target[ADMIN_USER_ID] = member.id

            # ส่งข้อความไปหาแอดมินใน DM พร้อมเมนูดรอปดาวน์เลือกตัวละคร
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
                    embed.set_footer(text="คุณสามารถใช้เมนูดรอปดาวน์ด้านล่างเพื่อเปลี่ยนคนที่จะตอบกลับได้")
                    
                    view = TargetSelectView(bot)
                    await admin_user.send(embed=embed, view=view)
            except Exception as e:
                print(f"Error sending DM to admin: {e}")

        return

    # กรณีที่ 2: ข้อความส่งมาทาง Direct Message (DM) หาบอทจากแอดมิน
    if message.guild is None and message.author.id == ADMIN_USER_ID:
        content = message.content
        
        # ตรวจสอบว่าแอดมินกำลังเลือกคุยกับใครอยู่
        target_member_id = admin_active_target.get(ADMIN_USER_ID)
        
        if not target_member_id:
            # ถ้ายังไม่ได้เลือก ให้ดึงคนล่าสุดจากฐานข้อมูลมาตอบแทน
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('SELECT member_id FROM chat_sessions ORDER BY timestamp DESC LIMIT 1')
            result = cursor.fetchone()
            conn.close()
            if result:
                target_member_id = result[0]
                admin_active_target[ADMIN_USER_ID] = target_member_id

        if target_member_id:
            try:
                guild = bot.get_guild(TARGET_GUILD_ID)
                target_member = guild.get_member(target_member_id) if guild else None
                support_channel_id = get_support_channel(TARGET_GUILD_ID)
                support_channel = guild.get_channel(support_channel_id) if guild else None

                if support_channel and target_member:
                    embed = discord.Embed(
                        description=content,
                        color=discord.Color.gold(),
                        timestamp=datetime.now()
                    )
                    embed.set_author(name="ฝ่ายสนับสนุน (Support)", icon_url=bot.user.display_avatar.url)
                    await support_channel.send(content=f"{target_member.mention}", embed=embed)
                    await message.add_reaction("✅")
                else:
                    await message.channel.send("⚠️ ไม่พบห้องซัพพอร์ตหรือสมาชิกคนดังกล่าวในเซิร์ฟเวอร์")
            except Exception as e:
                print(f"Error forwarding admin message: {e}")
                await message.channel.send("⚠️ เกิดข้อผิดพลาดในการส่งข้อความ")
        else:
            await message.channel.send("⚠️ ยังไม่มีเป้าหมายที่เลือก กรุณาให้สมาชิกทักมาก่อน หรือใช้คำสั่งเลือกผู้ใช้")

# --- Slash Command: /online ---
@tree.command(name="online", description="เช็คเวลาออนไลน์ในห้องเสียง (ย้อนหลัง 30 วัน)", guild=TARGET_GUILD)
@app_commands.describe(member="เลือกสมาชิกที่ต้องการเช็คเวลา")
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

# --- Slash Command: /รอแก้ ---
@tree.command(name="รอแก้", description="ตั้งค่าห้องสำหรับระบบคุยผ่านบอท (เฉพาะผู้ดูแล)", guild=TARGET_GUILD)
@app_commands.describe(channel="เลือกห้องแชทที่ต้องการเปิดระบบคุยผ่านบอท")
async def setup_support(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message("คุณไม่มีสิทธิ์ใช้งานคำสั่งนี้", ephemeral=True)
        return

    set_support_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message("«✅ เปิดระบบคุยผ่านบอทเรียบร้อยแล้ว»", ephemeral=True)

# --- Slash Command: /ตามบอท ---
@tree.command(name="ตามบอท", description="ส่งแจ้งเตือนเรียกผู้ดูแลระบบผ่านบอท", guild=TARGET_GUILD)
async def call_admin(interaction: discord.Interaction):
    if interaction.guild_id != TARGET_GUILD_ID:
        await interaction.response.send_message("คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์ที่กำหนดเท่านั้น", ephemeral=True)
        return

    member = interaction.user
    guild = interaction.guild
    channel = interaction.channel
    current_time = datetime.now().strftime("%H:%M")

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

    await interaction.response.send_message("✅ ส่งคำขอเรียกว่าบอทไปยังผู้ดูแลเรียบร้อยแล้ว", ephemeral=True)

bot.run(os.getenv("DISCORD_TOKEN"))
