import sqlite3
import requests
import asyncio
import re
import random
import json
import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session
from functools import wraps
import threading
from telethon import TelegramClient, events
from telethon.tl.custom import Button
from telethon import functions

# ==============================
# Paths — Auto-detect Termux vs Render
# Termux: current directory (./data)
# Render: /tmp/paidlike_data (or DATA_DIR env)
# ==============================
def get_data_dir():
    """Auto-detect best data directory based on environment"""
    # If DATA_DIR env is set (Render), use that
    env_dir = os.environ.get("DATA_DIR", "")
    if env_dir:
        return env_dir
    
    # Check if /tmp is writable (Linux/Render)
    try:
        os.makedirs("/tmp/paidlike_data_test", exist_ok=True)
        os.rmdir("/tmp/paidlike_data_test")
        return "/tmp/paidlike_data"
    except (OSError, PermissionError):
        pass
    
    # Termux / Android — use current script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_data = os.path.join(script_dir, "data")
    return local_data

DATA_DIR = get_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "paidlike.db")
SESSION_PATH = os.path.join(DATA_DIR, "my_telegram_session")

print(f"[INFO] Data directory: {DATA_DIR}")
print(f"[INFO] Database path: {DB_PATH}")
print(f"[INFO] Session path: {SESSION_PATH}")

# ==============================
# Database Setup
# ==============================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS paidlike_history(
    uid TEXT PRIMARY KEY,
    like_before INTEGER,
    like_after INTEGER,
    like_added INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bots(
    bot_token TEXT PRIMARY KEY,
    bot_username TEXT,
    bot_code TEXT UNIQUE,
    is_active INTEGER DEFAULT 1,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    active_days INTEGER DEFAULT 0,
    expiry_date TEXT,
    allowed_groups TEXT DEFAULT '',
    required_channels TEXT DEFAULT '',
    like_limit INTEGER DEFAULT 0,
    help_limit INTEGER DEFAULT 0,
    like_daily_count INTEGER DEFAULT 0,
    help_daily_count INTEGER DEFAULT 0,
    total_likes INTEGER DEFAULT 0,
    last_count_reset TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
)
""")

conn.commit()


# ==============================
# Database Helper Functions
# ==============================
def save_like(uid, liked):
    cursor.execute(
        "INSERT OR REPLACE INTO paidlike_history(uid, like_before) VALUES(?, ?)",
        (uid, liked)
    )
    conn.commit()


def get_like(uid):
    cursor.execute(
        "SELECT like_before FROM paidlike_history WHERE uid=?",
        (uid,)
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def update_like(uid, after, added):
    cursor.execute(
        "UPDATE paidlike_history SET like_after=?, like_added=? WHERE uid=?",
        (after, added, uid)
    )
    conn.commit()


def get_total_likes():
    cursor.execute("SELECT SUM(like_added) FROM paidlike_history")
    row = cursor.fetchone()
    return row[0] if row and row[0] else 0


def get_total_uids():
    cursor.execute("SELECT COUNT(*) FROM paidlike_history")
    row = cursor.fetchone()
    return row[0] if row else 0


def get_all_history():
    cursor.execute("SELECT uid, like_before, like_after, like_added FROM paidlike_history ORDER BY like_added DESC")
    rows = cursor.fetchall()
    return rows


def generate_bot_code():
    """Generate unique 8-digit code for a bot"""
    while True:
        code = str(random.randint(10000000, 99999999))
        cursor.execute("SELECT bot_code FROM bots WHERE bot_code=?", (code,))
        if not cursor.fetchone():
            return code


def save_bot_token(bot_token, bot_username, bot_code, active_days=0, allowed_groups='',
                   required_channels='', like_limit=0, help_limit=0):
    expiry_date = ''
    if active_days and int(active_days) > 0:
        expiry_date = (datetime.now() + timedelta(days=int(active_days))).strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute(
        """INSERT OR REPLACE INTO bots(bot_token, bot_username, bot_code, is_active, active_days, 
           expiry_date, allowed_groups, required_channels, like_limit, help_limit,
           like_daily_count, help_daily_count, total_likes, last_count_reset)
           VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?)""",
        (bot_token, bot_username, bot_code, active_days, expiry_date, allowed_groups,
         required_channels, like_limit, help_limit, datetime.now().strftime('%Y-%m-%d'))
    )
    conn.commit()


def get_all_bots():
    cursor.execute("SELECT bot_token, bot_username, bot_code, is_active, added_at, active_days, expiry_date, allowed_groups, required_channels, like_limit, help_limit, like_daily_count, help_daily_count, total_likes, last_count_reset FROM bots")
    return cursor.fetchall()


def get_bot_by_token(bot_token):
    cursor.execute("SELECT * FROM bots WHERE bot_token=?", (bot_token,))
    return cursor.fetchone()


def get_bot_by_code(bot_code):
    cursor.execute("SELECT * FROM bots WHERE bot_code=?", (bot_code,))
    return cursor.fetchone()


def delete_bot(bot_token):
    cursor.execute("DELETE FROM bots WHERE bot_token=?", (bot_token,))
    conn.commit()


def update_bot_field(bot_token, field, value):
    cursor.execute(f"UPDATE bots SET {field}=? WHERE bot_token=?", (value, bot_token))
    conn.commit()


def check_and_reset_daily_count(bot_token):
    """Reset daily command counts if it's a new day"""
    today = datetime.now().strftime('%Y-%m-%d')
    cursor.execute("SELECT last_count_reset FROM bots WHERE bot_token=?", (bot_token,))
    row = cursor.fetchone()
    if row:
        last_reset = row[0]
        if last_reset != today:
            cursor.execute(
                "UPDATE bots SET like_daily_count=0, help_daily_count=0, last_count_reset=? WHERE bot_token=?",
                (today, bot_token)
            )
            conn.commit()


def is_bot_expired(bot_token):
    """Check if bot has expired"""
    cursor.execute("SELECT expiry_date FROM bots WHERE bot_token=?", (bot_token,))
    row = cursor.fetchone()
    if row and row[0]:
        try:
            expiry = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
            return datetime.now() > expiry
        except:
            return False
    return False  # No expiry = unlimited


def increment_like_count(bot_token):
    """Increment daily like count and total likes for a bot"""
    check_and_reset_daily_count(bot_token)
    cursor.execute(
        "UPDATE bots SET like_daily_count=like_daily_count+1, total_likes=total_likes+1 WHERE bot_token=?",
        (bot_token,)
    )
    conn.commit()


def increment_help_count(bot_token):
    """Increment daily help count for a bot"""
    check_and_reset_daily_count(bot_token)
    cursor.execute(
        "UPDATE bots SET help_daily_count=help_daily_count+1 WHERE bot_token=?",
        (bot_token,)
    )
    conn.commit()


def can_use_like_command(bot_token):
    """Check if like command can be used today (rate limit)"""
    check_and_reset_daily_count(bot_token)
    cursor.execute("SELECT like_limit, like_daily_count FROM bots WHERE bot_token=?", (bot_token,))
    row = cursor.fetchone()
    if row:
        limit, count = row
        if limit == 0:  # 0 = unlimited
            return True
        return count < limit
    return True


def can_use_help_command(bot_token):
    """Check if help command can be used today (rate limit)"""
    check_and_reset_daily_count(bot_token)
    cursor.execute("SELECT help_limit, help_daily_count FROM bots WHERE bot_token=?", (bot_token,))
    row = cursor.fetchone()
    if row:
        limit, count = row
        if limit == 0:  # 0 = unlimited
            return True
        return count < limit
    return True


# ==============================
# Settings functions
# ==============================
DEFAULT_SETTINGS = {
    "sleep_seconds": "10",
    "source_group": "ff_like_bot_2025",
    "vip_group": "Community_FFBD",
    "vip_reply_timeout": "90",
    "dashboard_password": "admin123",
}

# Owner's required channel — cannot be removed by anyone
OWNER_REQUIRED_CHANNEL = "siam_bhai_official"


def get_setting(key):
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cursor.fetchone()
    if row:
        return row[0]
    default = DEFAULT_SETTINGS.get(key, "")
    if default:
        set_setting(key, default)
    return default


def set_setting(key, value):
    cursor.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
        (key, str(value))
    )
    conn.commit()


def get_all_settings():
    settings = {}
    for key in DEFAULT_SETTINGS:
        settings[key] = get_setting(key)
    return settings


# ==============================
# Telegram API তথ্য
# ==============================
API_ID = 39111583
API_HASH = "d628a5ae837ff4d16c104f2bef3c5b2e"
SESSION_NAME = SESSION_PATH

# /paidlike uid ধরার regex
PAIDLIKE_RE = re.compile(r"^/paidlike\s+(.+)$", re.IGNORECASE)
# /like uid or /like region uid regex
LIKE_RE = re.compile(r"^/like(?:\s+(.+))?$", re.IGNORECASE)
# /help command regex
HELP_RE = re.compile(r"^/help$", re.IGNORECASE)

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

app = Flask(__name__)
app.secret_key = "paidlike_super_secret_key_2025"

# Active bot clients dict: {bot_token: TelegramClient}
bot_clients = {}

# Like processing queue lock
like_queue_lock = asyncio.Lock()
like_queue_counter = 0


# ==============================
# Login Required Decorator
# ==============================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('dashboard_logged_in'):
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function


# ==============================
# Parse UID from /like argument
# ==============================
def parse_uid_from_like(arg):
    """
    Parse UID from /like argument.
    - /like 123456789 → uid = 123456789
    - /like BD 123456789 → uid = 123456789 (region stripped)
    - /like region123456789 → extract numeric part
    """
    if not arg:
        return None
    arg = arg.strip()
    parts = arg.split()
    for part in reversed(parts):
        if part.isdigit():
            return part
    digits = re.findall(r'\d+', arg)
    if digits:
        return max(digits, key=len)
    return None


# ==============================
# VIP reply wait function
# ==============================
async def wait_for_vip_reply(sent_message, timeout=90):
    vip_group = get_setting("vip_group")
    future = asyncio.get_event_loop().create_future()

    @client.on(events.NewMessage(chats=vip_group))
    async def handler(event):
        if future.done():
            return
        if event.message.id == sent_message.id:
            return
        if event.message.reply_to_msg_id == sent_message.id:
            future.set_result(event.message)
            return
        if event.message.id > sent_message.id and not event.out:
            future.set_result(event.message)

    try:
        return await asyncio.wait_for(future, timeout=timeout)
    finally:
        client.remove_event_handler(handler, events.NewMessage(chats=vip_group))


# ==============================
# Check if user is member of required channels
# ==============================
async def check_required_channels(bot_client, user_id, bot_token):
    """Check if user has joined all required channels for this bot (including owner's channel)"""
    bot_data = get_bot_by_token(bot_token)
    if not bot_data:
        return True, []
    required_channels_str = bot_data[8]  # required_channels field

    # Always include owner's required channel
    all_channels = [OWNER_REQUIRED_CHANNEL]
    if required_channels_str:
        extra = [ch.strip() for ch in required_channels_str.split(',') if ch.strip()]
        for ch in extra:
            if ch.lower() != OWNER_REQUIRED_CHANNEL.lower():
                all_channels.append(ch)

    not_joined = []
    for channel in all_channels:
        is_joined = False
        try:
            # Try multiple methods to resolve and check membership
            channel_input = channel if channel.startswith('-') or channel.lstrip('-').isdigit() else f"@{channel}"
            
            # Method 1: ResolveChannel + GetParticipant
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                resolved = await bot_client(functions.channels.ResolveUsernameRequest(
                    username=channel.lstrip('@')
                ))
                for ch in resolved.chats:
                    try:
                        await bot_client.get_participant(ch, user_id)
                        is_joined = True
                        break
                    except Exception:
                        pass
                if is_joined:
                    continue
            except Exception:
                pass

            # Method 2: Direct get_entity + get_participant
            if not is_joined:
                try:
                    entity = await bot_client.get_entity(channel_input)
                    await bot_client.get_participant(entity, user_id)
                    is_joined = True
                except Exception:
                    pass

            # Method 3: Check via GetParticipantRequest directly
            if not is_joined:
                try:
                    await bot_client(functions.channels.GetParticipantRequest(
                        channel=channel_input,
                        participant=user_id
                    ))
                    is_joined = True
                except Exception:
                    pass

            if not is_joined:
                not_joined.append(channel)
        except Exception:
            not_joined.append(channel)
    if not_joined:
        return False, not_joined
    return True, []


# ==============================
# Core async paidlike logic
# ==============================
async def process_paidlike(uid, wait_callback=None):
    global like_queue_counter

    sleep_seconds = int(get_setting("sleep_seconds"))
    vip_group = get_setting("vip_group")
    vip_timeout = int(get_setting("vip_reply_timeout"))

    command = f"/like {uid}"
    url = f"https://info.killersharmabot.online/level?uid={uid}"

    like_queue_counter += 1
    my_position = like_queue_counter

    if like_queue_lock.locked():
        if wait_callback:
            await wait_callback(f"⏳ আরেকটি like request চলছে... অপেক্ষা করুন (Queue Position: {my_position})")

    async with like_queue_lock:
        response = requests.get(url)

        if response.status_code != 200:
            return {
                "success": False,
                "message": "API Error - প্রথম API call ব্যর্থ হয়েছে"
            }

        info = response.json()

        nickname = info["name"]
        level = info["level"]
        exp = info["exp"]
        like_before = info["likes"]

        save_like(uid, like_before)

        try:
            sent = await client.send_message(vip_group, command)
            vip_reply = await wait_for_vip_reply(sent, timeout=vip_timeout)
            await asyncio.sleep(sleep_seconds)

            response = requests.get(url)

            if response.status_code != 200:
                return {
                    "success": False,
                    "message": "দ্বিতীয়বার API থেকে তথ্য পাওয়া যায়নি"
                }

            info = response.json()

            like_after = info["likes"]
            like_before_db = get_like(uid)
            like_added = like_after - like_before_db

            update_like(uid, like_after, like_added)

            vip_reply_text = vip_reply.text if vip_reply and vip_reply.text else None

            return {
                "success": True,
                "name": nickname,
                "uid": uid,
                "level": level,
                "exp": exp,
                "like": like_after,
                "like_before": like_before_db,
                "like_after": like_after,
                "like_added": like_added,
                "vip_reply": vip_reply_text
            }

        except asyncio.TimeoutError:
            return {
                "success": False,
                "message": f"VIP group থেকে {vip_timeout} seconds-এর মধ্যে কোনো reply পাওয়া যায়নি"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Error: {str(e)}"
            }


# ==============================
# LIKE_HELP_TEXT
# ==============================
LIKE_HELP_TEXT = """
━━━━━━━━━━━━━━━━━━━━━━
📌 /like কমান্ড ব্যবহার বিধি
━━━━━━━━━━━━━━━━━━━━━━

‣ /like {UID}
   উদাহরণ: /like 123456789

‣ /like {Region} {UID}
   উদাহরণ: /like BD 123456789
   (Region থাকলেও শুধু UID নেওয়া হবে)

❌ শুধু /like লিখলে কাজ করবে না
   UID অবশ্যই দিতে হবে

━━━━━━━━━━━━━━━━━━━━━━"""

HELP_TEXT = """
━━━━━━━━━━━━━━━━━━━━━━
📖 কমান্ড সাহায্য তালিকা
━━━━━━━━━━━━━━━━━━━━━━

‣ /like {UID}
   আপনার UID তে like যোগ করে
   উদাহরণ: /like 123456789

‣ /like {Region} {UID}
   Region সহ UID দিলেও কাজ করে
   উদাহরণ: /like BD 123456789

‣ /help
   সব কমান্ডের কাজ দেখায়

━━━━━━━━━━━━━━━━━━━━━━"""


# ==============================
# Bot Management — বট add/remove
# ==============================
async def start_bot_client(bot_token):
    if bot_token in bot_clients:
        return True

    try:
        bot_client = TelegramClient(
            f"{DATA_DIR}/bot_{bot_token[:10]}",
            API_ID,
            API_HASH
        )
        await bot_client.start(bot_token=bot_token)

        me = await bot_client.get_me()
        bot_username = me.username

        existing = get_bot_by_token(bot_token)
        if not existing:
            bot_code = generate_bot_code()
            save_bot_token(bot_token, bot_username, bot_code)
        else:
            update_bot_field(bot_token, 'bot_username', bot_username)

        bot_data = get_bot_by_token(bot_token)
        allowed_groups_str = bot_data[7] if bot_data else ''
        allowed_groups = [g.strip() for g in allowed_groups_str.split(',') if g.strip()]

        # Register /like handler
        @bot_client.on(events.NewMessage(pattern=LIKE_RE))
        async def bot_like_handler(event):
            bot_data = get_bot_by_token(bot_token)
            if not bot_data:
                await event.reply("❌ বট পাওয়া যায়নি!")
                return

            is_active = bot_data[3]
            if not is_active:
                await event.reply("🚫 This bot is currently off.")
                return

            if is_bot_expired(bot_token):
                await event.reply("⏰ এই বট এর সক্রিয় সময় শেষ হয়ে গেছে!")
                return

            arg = event.pattern_match.group(1)

            if not arg or not arg.strip():
                await event.reply(LIKE_HELP_TEXT)
                return

            # Check allowed groups
            allowed_groups_str = bot_data[7]
            if allowed_groups_str.strip():
                allowed_groups = [g.strip() for g in allowed_groups_str.split(',') if g.strip()]
                chat = await event.get_chat()
                chat_username = getattr(chat, 'username', '') or ''
                chat_id = str(chat.id)
                is_allowed = False
                for ag in allowed_groups:
                    if chat_username.lower() == ag.lower() or chat_id == ag or str(chat.id) == ag:
                        is_allowed = True
                        break
                if not is_allowed:
                    allowed_display = '\n'.join([f"• @{g}" if not g.startswith('-') else f"• {g}" for g in allowed_groups])
                    await event.reply(
                        f"🚫 এই গ্রুপে বট ব্যবহার করার অনুমতি নেই!\n\n"
                        f"📢 অনুমোদিত গ্রুপসমূহ:\n{allowed_display}\n\n"
                        f"শুধুমাত্র অনুমোদিত গ্রুপে বট ব্যবহার করতে পারবেন।"
                    )
                    return

            # Check required channels
            user_id = event.sender_id
            joined, not_joined = await check_required_channels(bot_client, user_id, bot_token)
            if not joined:
                channel_list = '\n'.join([f"• {ch}" for ch in not_joined])
                # Build inline join buttons for each channel
                buttons = []
                for ch in not_joined:
                    ch_clean = ch.lstrip('@')
                    buttons.append([Button.url(f"📢 Join {ch}", f"https://t.me/{ch_clean}")])
                await event.reply(
                    f"🚫 প্রয়োজনীয় চ্যানেলে যেন নেই!\n\n"
                    f"নিচের চ্যানেলগুলোতে যেন করুন:\n{channel_list}\n\n"
                    f"👇 Join বাটনে ক্লিক করে যেন হয়ে আবার চেষ্টা করুন।",
                    buttons=buttons
                )
                return

            # Check like command rate limit
            if not can_use_like_command(bot_token):
                bot_data2 = get_bot_by_token(bot_token)
                limit = bot_data2[9]
                await event.reply(f"⚠️ আজকের like কমান্ড সীমা ({limit} বার) শেষ হয়ে গেছে!")
                return

            uid = parse_uid_from_like(arg.strip())

            if not uid:
                await event.reply("❌ সঠিক UID দিন। উদাহরণ: /like 123456789")
                return

            increment_like_count(bot_token)

            processing_msg = await event.reply("⏳ Processing your like request...")

            async def bot_wait_cb(msg):
                try:
                    await processing_msg.edit(msg)
                except Exception:
                    pass

            result = await process_paidlike(uid, wait_callback=bot_wait_cb)

            if result["success"]:
                reply_text = f"""
━━━━━━━━━━━━━━━━━━━━━━
👤 Nickname : {result['name']}
🔑 UID : {result['uid']}

🏆 Level : {result['level']}
⭐ EXP : {result['exp']}

❤️ Like Before : {result['like_before']}
❤️ Like After : {result['like_after']}
➕ Like Added : {result['like_added']}
━━━━━━━━━━━━━━━━━━━━━━"""
                await event.reply(reply_text)
            else:
                await event.reply(f"❌ {result['message']}")

            try:
                await processing_msg.delete()
            except Exception:
                pass

        # Register /help handler
        @bot_client.on(events.NewMessage(pattern=HELP_RE))
        async def bot_help_handler(event):
            bot_data = get_bot_by_token(bot_token)
            if not bot_data:
                await event.reply("❌ বট পাওয়া যায়নি!")
                return

            is_active = bot_data[3]
            if not is_active:
                await event.reply("🚫 This bot is currently off.")
                return

            if not can_use_help_command(bot_token):
                bot_data2 = get_bot_by_token(bot_token)
                limit = bot_data2[10]
                await event.reply(f"⚠️ আজকের help কমান্ড সীমা ({limit} বার) শেষ হয়ে গেছে!")
                return

            increment_help_count(bot_token)
            await event.reply(HELP_TEXT)

        bot_clients[bot_token] = bot_client
        print(f"Bot @{bot_username} started successfully!")
        return True

    except Exception as e:
        print(f"Bot start error: {e}")
        return False


async def stop_bot_client(bot_token):
    if bot_token in bot_clients:
        try:
            await bot_clients[bot_token].disconnect()
        except Exception:
            pass
        del bot_clients[bot_token]
    delete_bot(bot_token)


async def start_all_saved_bots():
    bots = get_all_bots()
    for bot in bots:
        bot_token = bot[0]
        await start_bot_client(bot_token)


# ==============================
# Telegram group handler — Source group
# ==============================
@client.on(events.NewMessage(pattern=PAIDLIKE_RE))
async def paidlike_handler(event):
    source_group = get_setting("source_group")
    chat = await event.get_chat()
    chat_username = getattr(chat, 'username', '') or ''
    if chat_username.lower() != source_group.lower() and str(chat.id) != source_group:
        return

    uid = event.pattern_match.group(1).strip()

    if not uid:
        await event.reply("UID দিন। উদাহরণ: /paidlike 123456789")
        return

    processing_msg = await event.reply("⏳ Processing your paidlike request...")

    async def group_wait_cb(msg):
        try:
            await processing_msg.edit(msg)
        except Exception:
            pass

    result = await process_paidlike(uid, wait_callback=group_wait_cb)

    if result["success"]:
        vip_reply_text = result.get("vip_reply", "")
        reply_text = f"""{vip_reply_text if vip_reply_text else ""}

━━━━━━━━━━━━━━━━━━━━━━
👤 Nickname : {result['name']}
🔑 UID : {result['uid']}

🏆 Level : {result['level']}
⭐ EXP : {result['exp']}

❤️ Like Before : {result['like_before']}
❤️ Like After : {result['like_after']}
➕ Like Added : {result['like_added']}
━━━━━━━━━━━━━━━━━━━━━━"""
        await event.reply(reply_text)
    else:
        await event.reply(f"❌ {result['message']}")

    try:
        await processing_msg.delete()
    except Exception:
        pass


# ==============================
# Flask HTML Templates
# ==============================

# --- Login Page ---
LOGIN_HTML = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🔐 Dashboard Login</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
            min-height: 100vh;
            display: flex; align-items: center; justify-content: center;
            color: #fff; padding: 20px;
        }
        .card {
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 20px; padding: 40px;
            width: 400px; max-width: 95vw;
            backdrop-filter: blur(15px);
        }
        h2 { text-align: center; font-size: 1.8em; margin-bottom: 8px; }
        .subtitle { text-align: center; color: #aaa; margin-bottom: 30px; font-size: 0.9em; }
        .lock-icon { text-align: center; font-size: 3em; margin-bottom: 15px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; font-weight: 600; color: #ddd; }
        input[type="password"] {
            width: 100%; padding: 14px 16px;
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 10px; background: rgba(0,0,0,0.3);
            color: #fff; font-size: 1.1em;
            transition: border-color 0.3s;
        }
        input[type="password"]:focus {
            outline: none; border-color: #ff6b35;
            box-shadow: 0 0 15px rgba(255, 107, 53, 0.2);
        }
        input::placeholder { color: #666; }
        .btn-login {
            width: 100%; padding: 14px;
            background: linear-gradient(135deg, #ff6b35, #ff3e3e);
            color: white; border: none; border-radius: 10px;
            font-size: 1.1em; font-weight: 600;
            cursor: pointer; transition: all 0.3s;
            box-shadow: 0 5px 25px rgba(255, 107, 53, 0.3);
        }
        .btn-login:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 35px rgba(255, 107, 53, 0.5);
        }
        .error {
            text-align: center; padding: 12px; border-radius: 8px;
            margin-bottom: 20px; font-size: 0.95em;
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #ef4444;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="lock-icon">🔐</div>
        <h2>Dashboard Login</h2>
        <p class="subtitle">পাসওয়ার্ড দিয়ে ড্যাশবোর্ডে প্রবেশ করুন</p>

        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}

        <form action="/login" method="POST">
            <div class="form-group">
                <label>🔑 Password</label>
                <input type="password" name="password" placeholder="পাসওয়ার্ড দিন" required autofocus>
            </div>
            <button type="submit" class="btn-login">🔓 Login</button>
        </form>
    </div>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🔥 PaidLike Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
            min-height: 100vh;
            color: #fff;
        }
        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 30px 20px;
        }
        .top-bar {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 20px;
        }
        .btn-logout {
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid rgba(239, 68, 68, 0.4);
            color: #ef4444; padding: 8px 18px;
            border-radius: 8px; cursor: pointer;
            transition: all 0.3s; font-size: 0.85em;
            text-decoration: none;
        }
        .btn-logout:hover { background: rgba(239, 68, 68, 0.4); }
        h1 {
            text-align: center;
            font-size: 2.2em;
            margin-bottom: 10px;
            text-shadow: 0 0 20px rgba(255, 107, 53, 0.6);
        }
        .subtitle {
            text-align: center;
            color: #aaa;
            margin-bottom: 40px;
            font-size: 0.95em;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }
        .stat-card {
            background: rgba(255,255,255,0.07);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            padding: 25px;
            text-align: center;
            backdrop-filter: blur(10px);
            transition: transform 0.3s, box-shadow 0.3s;
        }
        .stat-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 40px rgba(255, 107, 53, 0.2);
        }
        .stat-icon { font-size: 2.5em; margin-bottom: 10px; }
        .stat-value {
            font-size: 2.8em;
            font-weight: bold;
            color: #ff6b35;
            text-shadow: 0 0 15px rgba(255, 107, 53, 0.4);
        }
        .stat-label { color: #bbb; margin-top: 5px; font-size: 0.95em; }
        .section-title {
            font-size: 1.5em;
            margin-bottom: 20px;
            padding-left: 10px;
            border-left: 4px solid #ff6b35;
        }
        .history-table {
            width: 100%;
            border-collapse: collapse;
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            overflow: hidden;
            margin-bottom: 40px;
        }
        .history-table th {
            background: rgba(255, 107, 53, 0.3);
            padding: 14px 16px;
            text-align: left;
            font-weight: 600;
        }
        .history-table td {
            padding: 12px 16px;
            border-bottom: 1px solid rgba(255,255,255,0.06);
        }
        .history-table tr:hover td { background: rgba(255,255,255,0.04); }
        .like-added { color: #4ade80; font-weight: bold; }
        .btn-add-bot {
            display: inline-block;
            background: linear-gradient(135deg, #ff6b35, #ff3e3e);
            color: white;
            border: none;
            padding: 14px 32px;
            font-size: 1.1em;
            border-radius: 50px;
            cursor: pointer;
            transition: all 0.3s;
            text-decoration: none;
            font-weight: 600;
            box-shadow: 0 5px 25px rgba(255, 107, 53, 0.4);
        }
        .btn-add-bot:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 35px rgba(255, 107, 53, 0.6);
        }
        .btn-center { text-align: center; margin-bottom: 40px; }
        .bot-list { margin-bottom: 40px; }
        .bot-card {
            background: rgba(255,255,255,0.07);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 18px 22px;
            margin-bottom: 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            backdrop-filter: blur(10px);
            flex-wrap: wrap;
            gap: 10px;
        }
        .bot-info { display: flex; align-items: center; gap: 12px; }
        .bot-avatar {
            width: 42px; height: 42px;
            background: linear-gradient(135deg, #ff6b35, #ff3e3e);
            border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 1.2em;
        }
        .bot-name { font-weight: 600; font-size: 1.05em; }
        .bot-token-display { color: #888; font-size: 0.85em; font-family: monospace; }
        .bot-status { font-size: 0.8em; margin-top: 2px; }
        .bot-status.active { color: #4ade80; }
        .bot-status.inactive { color: #ef4444; }
        .bot-status.expired { color: #f59e0b; }
        .bot-actions { display: flex; gap: 8px; flex-wrap: wrap; }
        .btn-dashboard {
            background: rgba(59, 130, 246, 0.2);
            border: 1px solid rgba(59, 130, 246, 0.4);
            color: #3b82f6;
            padding: 8px 18px;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.3s;
            font-size: 0.9em;
            text-decoration: none;
        }
        .btn-dashboard:hover { background: rgba(59, 130, 246, 0.4); }
        .btn-remove {
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid rgba(239, 68, 68, 0.4);
            color: #ef4444;
            padding: 8px 18px;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.3s;
            font-size: 0.9em;
        }
        .btn-remove:hover { background: rgba(239, 68, 68, 0.4); }
        .empty-state { text-align: center; padding: 40px; color: #777; }
        .empty-state .icon { font-size: 3em; margin-bottom: 15px; }
        .api-info {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 30px;
        }
        .api-info h3 { margin-bottom: 10px; color: #ff6b35; }
        .api-info code {
            background: rgba(0,0,0,0.3);
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 0.9em;
        }
        .api-info p { margin: 8px 0; color: #ccc; line-height: 1.6; }
        .settings-section {
            background: rgba(255,255,255,0.07);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            padding: 25px;
            margin-bottom: 40px;
            backdrop-filter: blur(10px);
        }
        .settings-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .setting-item {
            background: rgba(0,0,0,0.2);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            padding: 18px;
        }
        .setting-item label {
            display: block; margin-bottom: 8px;
            font-weight: 600; color: #ddd; font-size: 0.95em;
        }
        .setting-item .setting-desc {
            color: #888; font-size: 0.82em; margin-bottom: 10px;
        }
        .setting-item input[type="text"],
        .setting-item input[type="number"],
        .setting-item input[type="password"] {
            width: 100%; padding: 12px 14px;
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 8px;
            background: rgba(0,0,0,0.3);
            color: #fff; font-size: 1em;
            transition: border-color 0.3s;
        }
        .setting-item input:focus {
            outline: none; border-color: #ff6b35;
            box-shadow: 0 0 10px rgba(255, 107, 53, 0.2);
        }
        .btn-save-settings {
            display: block; width: 100%; padding: 14px;
            background: linear-gradient(135deg, #ff6b35, #ff3e3e);
            color: white; border: none; border-radius: 10px;
            font-size: 1.1em; font-weight: 600;
            cursor: pointer; transition: all 0.3s;
            box-shadow: 0 5px 25px rgba(255, 107, 53, 0.3);
        }
        .btn-save-settings:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 35px rgba(255, 107, 53, 0.5);
        }
        .settings-message {
            text-align: center; padding: 12px; border-radius: 8px;
            margin-top: 15px; font-size: 0.95em; display: none;
        }
        .settings-message.success {
            background: rgba(74, 222, 128, 0.15);
            border: 1px solid rgba(74, 222, 128, 0.3);
            color: #4ade80; display: block;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="top-bar">
            <span></span>
            <a href="/logout" class="btn-logout">🚪 Logout</a>
        </div>

        <h1>🔥 PaidLike Dashboard</h1>
        <p class="subtitle">Like request ম্যানেজমেন্ট প্যানেল</p>

        <!-- Stats Cards -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-icon">❤️</div>
                <div class="stat-value">{{ total_likes }}</div>
                <div class="stat-label">টোটাল Like দেওয়া হয়েছে</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">👥</div>
                <div class="stat-value">{{ total_uids }}</div>
                <div class="stat-label">টোটাল UID তে Request পাঠানো হয়েছে</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🤖</div>
                <div class="stat-value">{{ total_bots }}</div>
                <div class="stat-label">Active Bot</div>
            </div>
        </div>

        <!-- Settings Section -->
        <h2 class="section-title">⚙️ Settings</h2>
        <div class="settings-section">
            {% if settings_msg %}
            <div class="settings-message success">{{ settings_msg }}</div>
            {% endif %}
            <form action="/savesettings" method="POST">
                <div class="settings-grid">
                    <div class="setting-item">
                        <label>💤 Sleep Seconds</label>
                        <div class="setting-desc">Like রেজাল্ট আসার পর কত সেকেন্ড wait করবে (ডিফল্ট: 10)</div>
                        <input type="number" name="sleep_seconds" value="{{ settings.sleep_seconds }}" min="1" max="120">
                    </div>
                    <div class="setting-item">
                        <label>📢 Source Group</label>
                        <div class="setting-desc">যে গ্রুপে /paidlike কমান্ড শুনবে (username বা ID)</div>
                        <input type="text" name="source_group" value="{{ settings.source_group }}" placeholder="ff_like_bot_2025">
                    </div>
                    <div class="setting-item">
                        <label>👑 VIP Group</label>
                        <div class="setting-desc">Like কমান্ড পাঠানোর গ্রুপ (username বা ID)</div>
                        <input type="text" name="vip_group" value="{{ settings.vip_group }}" placeholder="Community_FFBD">
                    </div>
                    <div class="setting-item">
                        <label>⏱️ VIP Reply Timeout</label>
                        <div class="setting-desc">VIP গ্রুপে কত সেকেন্ড reply এর জন্য wait করবে (ডিফল্ট: 90)</div>
                        <input type="number" name="vip_reply_timeout" value="{{ settings.vip_reply_timeout }}" min="10" max="300">
                    </div>
                    <div class="setting-item">
                        <label>🔐 Dashboard Password</label>
                        <div class="setting-desc">ড্যাশবোর্ডে প্রবেশের পাসওয়ার্ড</div>
                        <input type="password" name="dashboard_password" value="{{ settings.dashboard_password }}" placeholder="পাসওয়ার্ড দিন">
                    </div>
                </div>
                <button type="submit" class="btn-save-settings">💾 Save Settings</button>
            </form>
        </div>

        <!-- API Info -->
        <div class="api-info">
            <h3>📡 API Endpoint</h3>
            <p><code>GET /like?uid={uid}</code> — UID দিয়ে like request পাঠান</p>
            <p><code>GET /api/stats</code> — JSON format এ stats দেখুন</p>
            <p><code>GET /api/history</code> — JSON format এ সব history দেখুন</p>
            <p><code>GET /api/settings</code> — JSON format এ সব settings দেখুন</p>
            <p><code>GET /bot</code> — বট স্ট্যাটাস পেজ (Bot Code দিয়ে লগইন)</p>
        </div>

        <!-- History Table -->
        <h2 class="section-title">📊 Like History</h2>
        {% if history %}
        <table class="history-table">
            <thead>
                <tr>
                    <th>UID</th>
                    <th>Like Before</th>
                    <th>Like After</th>
                    <th>Like Added</th>
                </tr>
            </thead>
            <tbody>
                {% for row in history %}
                <tr>
                    <td>{{ row[0] }}</td>
                    <td>{{ row[1] }}</td>
                    <td>{{ row[2] }}</td>
                    <td class="like-added">+{{ row[3] }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div class="empty-state">
            <div class="icon">📊</div>
            <p>কোনো like history পাওয়া যায়নি</p>
        </div>
        {% endif %}

        <!-- Add Bot Button -->
        <div class="btn-center">
            <a href="/addbot" class="btn-add-bot">🤖 Add Bot</a>
        </div>

        <!-- Bot List -->
        <h2 class="section-title">🤖 Active Bots</h2>
        {% if bots %}
        <div class="bot-list">
            {% for bot in bots %}
            <div class="bot-card">
                <div class="bot-info">
                    <div class="bot-avatar">🤖</div>
                    <div>
                        <div class="bot-name">@{{ bot[1] }}</div>
                        <div class="bot-token-display">{{ bot[0][:15] }}... | Code: {{ bot[2] }}</div>
                        {% if bot[5] %}
                        <div class="bot-status {% if bot[3] %}active{% else %}inactive{% endif %}">
                            {% if bot[3] %}✅ Active{% else %}❌ Inactive{% endif %}
                            {% if bot[6] %} | ⏰ Expires: {{ bot[6] }}{% endif %}
                        </div>
                        {% else %}
                        <div class="bot-status {% if bot[3] %}active{% else %}inactive{% endif %}">
                            {% if bot[3] %}✅ Active{% else %}❌ Inactive{% endif %}
                            | ♾️ Unlimited
                        </div>
                        {% endif %}
                    </div>
                </div>
                <div class="bot-actions">
                    <a href="/botdashboard/{{ bot[2] }}" class="btn-dashboard">📊 Dashboard</a>
                    <form action="/removebot" method="POST" style="display:inline;">
                        <input type="hidden" name="bot_token" value="{{ bot[0] }}">
                        <button type="submit" class="btn-remove">🗑 Remove</button>
                    </form>
                </div>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="empty-state">
            <div class="icon">🤖</div>
            <p>কোনো bot add করা হয়নি। "Add Bot" বাটনে ক্লিক করে bot add করুন।</p>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""

ADD_BOT_HTML = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🤖 Add Bot — PaidLike</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #fff;
            padding: 20px 0;
        }
        .card {
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 20px;
            padding: 40px;
            width: 550px;
            max-width: 95vw;
            backdrop-filter: blur(15px);
        }
        h2 { text-align: center; font-size: 1.8em; margin-bottom: 8px; }
        .subtitle { text-align: center; color: #aaa; margin-bottom: 30px; font-size: 0.9em; }
        .form-group { margin-bottom: 18px; }
        label { display: block; margin-bottom: 6px; font-weight: 600; color: #ddd; font-size: 0.9em; }
        .field-desc { color: #888; font-size: 0.78em; margin-bottom: 6px; }
        input[type="text"], input[type="number"], textarea {
            width: 100%; padding: 12px 14px;
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 8px;
            background: rgba(0,0,0,0.3);
            color: #fff; font-size: 0.95em;
            transition: border-color 0.3s;
        }
        input:focus, textarea:focus {
            outline: none; border-color: #ff6b35;
            box-shadow: 0 0 10px rgba(255, 107, 53, 0.2);
        }
        input::placeholder, textarea::placeholder { color: #666; }
        textarea { resize: vertical; min-height: 60px; font-family: inherit; }
        .btn-submit {
            width: 100%; padding: 14px;
            background: linear-gradient(135deg, #ff6b35, #ff3e3e);
            color: white; border: none; border-radius: 10px;
            font-size: 1.1em; font-weight: 600;
            cursor: pointer; transition: all 0.3s;
            box-shadow: 0 5px 25px rgba(255, 107, 53, 0.3);
        }
        .btn-submit:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 35px rgba(255, 107, 53, 0.5);
        }
        .btn-back {
            display: block; text-align: center;
            margin-top: 20px; color: #aaa;
            text-decoration: none; transition: color 0.3s;
        }
        .btn-back:hover { color: #ff6b35; }
        .message {
            text-align: center; padding: 12px; border-radius: 8px;
            margin-bottom: 20px; font-size: 0.95em;
        }
        .message.success {
            background: rgba(74, 222, 128, 0.15);
            border: 1px solid rgba(74, 222, 128, 0.3);
            color: #4ade80;
        }
        .message.error {
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #ef4444;
        }
        .info-box {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 10px;
            padding: 15px; margin-bottom: 25px;
            font-size: 0.85em; color: #bbb; line-height: 1.6;
        }
        .info-box strong { color: #ff6b35; }
        .divider {
            border: none; border-top: 1px solid rgba(255,255,255,0.1);
            margin: 20px 0;
        }
        .optional-label {
            color: #888; font-size: 0.75em; font-style: italic;
        }
    </style>
</head>
<body>
    <div class="card">
        <h2>🤖 Add Bot</h2>
        <p class="subtitle">Telegram Bot Token দিয়ে bot add করুন</p>

        {% if message %}
        <div class="message {{ msg_type }}">{{ message }}</div>
        {% endif %}

        <div class="info-box">
            <strong>📌 কিভাবে Bot Token পাবেন?</strong><br>
            1. Telegram-এ <strong>@BotFather</strong> এ যান<br>
            2. <code>/newbot</code> বা <code>/mybots</code> কমান্ড দিন<br>
            3. Bot Token কপি করে নিচে পেস্ট করুন<br><br>
            <strong>💡 ফাঁকা রাখলে ডিফল্টভাবে সব আনলিমিটেড থাকবে</strong>
        </div>

        <form action="/addbot" method="POST">
            <div class="form-group">
                <label for="bot_token">🔑 Bot Token <span style="color:#ef4444">*</span></label>
                <input type="text" id="bot_token" name="bot_token"
                       placeholder="যেমন: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz" required>
            </div>

            <hr class="divider">
            <p class="optional-label">নিচের ফিল্ডগুলো ঐচ্ছিক - ফাঁকা রাখলে আনলিমিটেড থাকবে</p>

            <div class="form-group">
                <label>📅 Active Days</label>
                <div class="field-desc">বট কতদিন active থাকবে (0 বা ফাঁকা = আনলিমিটেড)</div>
                <input type="number" name="active_days" placeholder="0 = Unlimited" min="0" value="0">
            </div>

            <div class="form-group">
                <label>❤️ Like Command Daily Limit</label>
                <div class="field-desc">প্রতিদিন /like কমান্ড কতবার ব্যবহার করা যাবে (0 = আনলিমিটেড)</div>
                <input type="number" name="like_limit" placeholder="0 = Unlimited" min="0" value="0">
            </div>

            <div class="form-group">
                <label>📖 Help Command Daily Limit</label>
                <div class="field-desc">প্রতিদিন /help কমান্ড কতবার ব্যবহার করা যাবে (0 = আনলিমিটেড)</div>
                <input type="number" name="help_limit" placeholder="0 = Unlimited" min="0" value="0">
            </div>

            <div class="form-group">
                <label>🔗 Allowed Groups</label>
                <div class="field-desc">যে গ্রুপগুলোতে বট কাজ করবে (কমা দিয়ে একাধিক group username/ID)। ফাঁকা = সব গ্রুপে কাজ করবে</div>
                <textarea name="allowed_groups" placeholder="group1, group2, -1001234567890"></textarea>
            </div>

            <div class="form-group">
                <label>📢 Required Channels</label>
                <div class="field-desc">ইউজারকে এই চ্যানেলে যেন থাকতে হবে (কমা দিয়ে একাধিক channel username)। ফাঁকা = কোনো চ্যানেল লাগবে না</div>
                <textarea name="required_channels" placeholder="channel1, channel2"></textarea>
            </div>

            <button type="submit" class="btn-submit">✅ Save & Start Bot</button>
        </form>

        <a href="/" class="btn-back">← Dashboard এ ফিরে যান</a>
    </div>
</body>
</html>
"""

BOT_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📊 Bot Dashboard — {{ bot_username }}</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
            min-height: 100vh;
            color: #fff;
        }
        .container { max-width: 900px; margin: 0 auto; padding: 30px 20px; }
        h1 { text-align: center; font-size: 2em; margin-bottom: 5px; text-shadow: 0 0 20px rgba(59, 130, 246, 0.6); }
        .bot-code-display {
            text-align: center; color: #3b82f6; font-size: 1.1em;
            margin-bottom: 25px; font-family: monospace;
        }
        .stats-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px; margin-bottom: 30px;
        }
        .stat-card {
            background: rgba(255,255,255,0.07);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 14px; padding: 20px;
            text-align: center; backdrop-filter: blur(10px);
        }
        .stat-icon { font-size: 2em; margin-bottom: 8px; }
        .stat-value { font-size: 2em; font-weight: bold; color: #3b82f6; }
        .stat-label { color: #bbb; margin-top: 4px; font-size: 0.85em; }
        .section-title {
            font-size: 1.4em; margin-bottom: 15px;
            padding-left: 10px; border-left: 4px solid #3b82f6;
        }
        .info-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 15px; margin-bottom: 30px;
        }
        .info-card {
            background: rgba(255,255,255,0.07);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px; padding: 18px;
        }
        .info-card .label { color: #888; font-size: 0.85em; margin-bottom: 5px; }
        .info-card .value { font-size: 1.2em; font-weight: 600; }
        .status-active { color: #4ade80; }
        .status-inactive { color: #ef4444; }
        .status-expired { color: #f59e0b; }
        .today-usage {
            background: rgba(255,255,255,0.07);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px; padding: 20px; margin-bottom: 30px;
        }
        .usage-bar-container {
            background: rgba(0,0,0,0.3); border-radius: 8px;
            height: 24px; margin: 8px 0; overflow: hidden;
        }
        .usage-bar {
            height: 100%; border-radius: 8px;
            transition: width 0.5s;
        }
        .usage-bar.like { background: linear-gradient(135deg, #ff6b35, #ff3e3e); }
        .usage-bar.help { background: linear-gradient(135deg, #3b82f6, #6366f1); }
        .usage-text { display: flex; justify-content: space-between; font-size: 0.85em; color: #aaa; }
        .btn-back {
            display: inline-block; color: #aaa; text-decoration: none;
            transition: color 0.3s; margin-bottom: 20px;
        }
        .btn-back:hover { color: #3b82f6; }
        .btn-toggle {
            padding: 10px 24px; border-radius: 8px;
            cursor: pointer; font-size: 0.95em; font-weight: 600;
            transition: all 0.3s; border: none;
        }
        .btn-toggle.on {
            background: rgba(74, 222, 128, 0.2);
            border: 1px solid rgba(74, 222, 128, 0.4);
            color: #4ade80;
        }
        .btn-toggle.off {
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid rgba(239, 68, 68, 0.4);
            color: #ef4444;
        }
        .toggle-section {
            text-align: center; margin-bottom: 30px;
        }
        .settings-section {
            background: rgba(255,255,255,0.07);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px; padding: 25px;
            margin-bottom: 30px; backdrop-filter: blur(10px);
        }
        .settings-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 15px; margin-bottom: 15px;
        }
        .setting-item {
            background: rgba(0,0,0,0.2);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 10px; padding: 15px;
        }
        .setting-item label {
            display: block; margin-bottom: 6px;
            font-weight: 600; color: #ddd; font-size: 0.88em;
        }
        .setting-item .field-desc {
            color: #888; font-size: 0.75em; margin-bottom: 8px;
        }
        .setting-item input, .setting-item textarea {
            width: 100%; padding: 10px 12px;
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 8px;
            background: rgba(0,0,0,0.3);
            color: #fff; font-size: 0.9em;
        }
        .setting-item textarea { resize: vertical; min-height: 50px; font-family: inherit; }
        .setting-item input:focus, .setting-item textarea:focus {
            outline: none; border-color: #3b82f6;
        }
        .btn-save {
            width: 100%; padding: 12px;
            background: linear-gradient(135deg, #3b82f6, #6366f1);
            color: white; border: none; border-radius: 10px;
            font-size: 1em; font-weight: 600;
            cursor: pointer; transition: all 0.3s;
        }
        .btn-save:hover { transform: translateY(-2px); }
        .msg {
            text-align: center; padding: 10px; border-radius: 8px;
            margin-top: 10px; font-size: 0.9em;
        }
        .msg.success {
            background: rgba(74, 222, 128, 0.15);
            border: 1px solid rgba(74, 222, 128, 0.3);
            color: #4ade80;
        }
    </style>
</head>
<body>
    <div class="container">
        <a href="/" class="btn-back">← মূল Dashboard</a>

        <h1>📊 Bot Dashboard</h1>
        <div class="bot-code-display">@{{ bot_username }} | Code: {{ bot_code }}</div>

        <!-- Toggle Bot On/Off -->
        <div class="toggle-section">
            {% if is_active %}
            <form action="/togglebot/{{ bot_code }}" method="POST" style="display:inline;">
                <input type="hidden" name="action" value="deactivate">
                <button type="submit" class="btn-toggle on">✅ Bot Active — Click to Deactivate</button>
            </form>
            {% else %}
            <form action="/togglebot/{{ bot_code }}" method="POST" style="display:inline;">
                <input type="hidden" name="action" value="activate">
                <button type="submit" class="btn-toggle off">❌ Bot Inactive — Click to Activate</button>
            </form>
            {% endif %}
        </div>

        <!-- Stats Cards -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-icon">❤️</div>
                <div class="stat-value">{{ total_likes }}</div>
                <div class="stat-label">মোট Like দেওয়া হয়েছে</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">📅</div>
                <div class="stat-value">{{ like_daily_count }}</div>
                <div class="stat-label">আজকের Like ব্যবহার</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">📖</div>
                <div class="stat-value">{{ help_daily_count }}</div>
                <div class="stat-label">আজকের Help ব্যবহার</div>
            </div>
        </div>

        <!-- Info Cards -->
        <h2 class="section-title">📋 Bot Info</h2>
        <div class="info-grid">
            <div class="info-card">
                <div class="label">🤖 Bot Username</div>
                <div class="value">@{{ bot_username }}</div>
            </div>
            <div class="info-card">
                <div class="label">📅 Added At</div>
                <div class="value">{{ added_at }}</div>
            </div>
            <div class="info-card">
                <div class="label">⏰ Active Days</div>
                <div class="value">{% if active_days > 0 %}{{ active_days }} Days{% else %}♾️ Unlimited{% endif %}</div>
            </div>
            <div class="info-card">
                <div class="label">📆 Expiry Date</div>
                <div class="value {% if is_expired %}status-expired{% elif expiry_date %}status-active{% endif %}">
                    {% if expiry_date %}{{ expiry_date }}{% else %}♾️ No Expiry{% endif %}
                    {% if is_expired %} (Expired!){% endif %}
                </div>
            </div>
            <div class="info-card">
                <div class="label">🔗 Allowed Groups</div>
                <div class="value" style="font-size:0.95em;">{% if allowed_groups %}{{ allowed_groups }}{% else %}🌐 All Groups{% endif %}</div>
            </div>
            <div class="info-card">
                <div class="label">📢 Required Channels</div>
                <div class="value" style="font-size:0.95em;">{% if required_channels %}{{ required_channels }}{% else %}None{% endif %}</div>
            </div>
        </div>

        <!-- Today's Usage -->
        <h2 class="section-title">📊 আজকের কমান্ড ব্যবহার</h2>
        <div class="today-usage">
            <div>
                <strong>❤️ /like কমান্ড:</strong> {{ like_daily_count }} / {% if like_limit > 0 %}{{ like_limit }}{% else %}♾️{% endif %}
                <div class="usage-bar-container">
                    <div class="usage-bar like" style="width: {{ like_usage_percent }}%;"></div>
                </div>
                <div class="usage-text">
                    <span>0</span>
                    <span>{% if like_limit > 0 %}{{ like_limit }}{% else %}Unlimited{% endif %}</span>
                </div>
            </div>
            <div style="margin-top: 20px;">
                <strong>📖 /help কমান্ড:</strong> {{ help_daily_count }} / {% if help_limit > 0 %}{{ help_limit }}{% else %}♾️{% endif %}
                <div class="usage-bar-container">
                    <div class="usage-bar help" style="width: {{ help_usage_percent }}%;"></div>
                </div>
                <div class="usage-text">
                    <span>0</span>
                    <span>{% if help_limit > 0 %}{{ help_limit }}{% else %}Unlimited{% endif %}</span>
                </div>
            </div>
        </div>

        <!-- Settings Section -->
        <h2 class="section-title">⚙️ Bot Settings</h2>
        <div class="settings-section">
            {% if settings_msg %}
            <div class="msg success">{{ settings_msg }}</div>
            {% endif %}
            <form action="/updatebotsettings/{{ bot_code }}" method="POST">
                <div class="settings-grid">
                    <div class="setting-item">
                        <label>📅 Active Days</label>
                        <div class="field-desc">0 = Unlimited</div>
                        <input type="number" name="active_days" value="{% if active_days > 0 %}{{ active_days }}{% else %}0{% endif %}" min="0">
                    </div>
                    <div class="setting-item">
                        <label>❤️ Like Daily Limit</label>
                        <div class="field-desc">0 = Unlimited</div>
                        <input type="number" name="like_limit" value="{% if like_limit > 0 %}{{ like_limit }}{% else %}0{% endif %}" min="0">
                    </div>
                    <div class="setting-item">
                        <label>📖 Help Daily Limit</label>
                        <div class="field-desc">0 = Unlimited</div>
                        <input type="number" name="help_limit" value="{% if help_limit > 0 %}{{ help_limit }}{% else %}0{% endif %}" min="0">
                    </div>
                    <div class="setting-item">
                        <label>🔗 Allowed Groups</label>
                        <div class="field-desc">কমা দিয়ে একাধিক। ফাঁকা = সব গ্রুপ</div>
                        <textarea name="allowed_groups">{{ allowed_groups }}</textarea>
                    </div>
                    <div class="setting-item">
                        <label>📢 Required Channels</label>
                        <div class="field-desc">কমা দিয়ে একাধিক। ফাঁকা = কোনো চ্যানেল লাগবে না</div>
                        <textarea name="required_channels">{{ required_channels }}</textarea>
                    </div>
                </div>
                <button type="submit" class="btn-save">💾 Update Settings</button>
            </form>
        </div>
    </div>
</body>
</html>
"""

# --- /bot Login + Status Page ---
BOT_LOGIN_HTML = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🤖 Bot Login</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
            min-height: 100vh;
            display: flex; align-items: center; justify-content: center;
            color: #fff; padding: 20px;
        }
        .card {
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 20px; padding: 40px;
            width: 420px; max-width: 95vw;
            backdrop-filter: blur(15px);
        }
        .bot-icon { text-align: center; font-size: 3.5em; margin-bottom: 15px; }
        h2 { text-align: center; font-size: 1.8em; margin-bottom: 8px; }
        .subtitle { text-align: center; color: #aaa; margin-bottom: 30px; font-size: 0.9em; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; font-weight: 600; color: #ddd; text-align: center; }
        input[type="text"] {
            width: 100%; padding: 14px; text-align: center;
            font-size: 1.4em; letter-spacing: 6px;
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 10px; background: rgba(0,0,0,0.3);
            color: #fff; transition: border-color 0.3s;
        }
        input[type="text"]:focus {
            outline: none; border-color: #3b82f6;
            box-shadow: 0 0 15px rgba(59, 130, 246, 0.3);
        }
        input::placeholder { color: #555; letter-spacing: 2px; font-size: 0.8em; }
        .btn-submit {
            width: 100%; padding: 14px;
            background: linear-gradient(135deg, #3b82f6, #6366f1);
            color: white; border: none; border-radius: 10px;
            font-size: 1.1em; font-weight: 600;
            cursor: pointer; transition: all 0.3s;
            box-shadow: 0 5px 25px rgba(59, 130, 246, 0.3);
        }
        .btn-submit:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 35px rgba(59, 130, 246, 0.5);
        }
        .error {
            text-align: center; padding: 12px; border-radius: 8px;
            margin-bottom: 20px; font-size: 0.95em;
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #ef4444;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="bot-icon">🤖</div>
        <h2>Bot Login</h2>
        <p class="subtitle">আপনার 8-সংখ্যার Bot Code দিয়ে লগইন করুন</p>

        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}

        <form action="/bot" method="POST">
            <div class="form-group">
                <label>🔑 Bot Code</label>
                <input type="text" name="code" placeholder="00000000" maxlength="8" pattern="[0-9]{8}" required autofocus>
            </div>
            <button type="submit" class="btn-submit">🔓 Login</button>
        </form>
    </div>
</body>
</html>
"""

# --- /bot Status Page (after login) ---
BOT_STATUS_HTML = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🤖 Bot Status — @{{ bot_username }}</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
            min-height: 100vh; color: #fff; padding: 20px 0;
        }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        h1 { text-align: center; font-size: 1.8em; margin-bottom: 5px; text-shadow: 0 0 20px rgba(59, 130, 246, 0.6); }
        .bot-code-display {
            text-align: center; color: #3b82f6; font-size: 1.1em;
            margin-bottom: 25px; font-family: monospace;
        }
        .btn-logout {
            display: inline-block; color: #aaa; text-decoration: none;
            transition: color 0.3s; margin-bottom: 20px;
            font-size: 0.9em;
        }
        .btn-logout:hover { color: #ef4444; }
        .stats-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 12px; margin-bottom: 25px;
        }
        .stat-card {
            background: rgba(255,255,255,0.07);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px; padding: 15px;
            text-align: center;
        }
        .stat-icon { font-size: 1.5em; margin-bottom: 5px; }
        .stat-value { font-size: 1.5em; font-weight: bold; color: #3b82f6; }
        .stat-label { color: #bbb; margin-top: 3px; font-size: 0.75em; }
        .status-info {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px; padding: 20px;
            margin-bottom: 20px;
        }
        .status-row {
            display: flex; justify-content: space-between;
            padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06);
        }
        .status-row:last-child { border-bottom: none; }
        .status-row .label { color: #888; }
        .status-row .value { font-weight: 600; }
        .toggle-section { text-align: center; margin: 20px 0; }
        .btn-toggle {
            padding: 10px 24px; border-radius: 8px;
            cursor: pointer; font-size: 0.95em; font-weight: 600;
            transition: all 0.3s; border: none;
        }
        .btn-toggle.on {
            background: rgba(74, 222, 128, 0.2);
            border: 1px solid rgba(74, 222, 128, 0.4);
            color: #4ade80;
        }
        .btn-toggle.off {
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid rgba(239, 68, 68, 0.4);
            color: #ef4444;
        }
        .settings-form { margin-top: 20px; }
        .section-title {
            font-size: 1.3em; margin-bottom: 15px;
            padding-left: 10px; border-left: 4px solid #3b82f6;
        }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; color: #ddd; font-size: 0.88em; }
        .form-group .field-desc { color: #888; font-size: 0.75em; margin-bottom: 5px; }
        .form-group input, .form-group textarea {
            width: 100%; padding: 10px; border: 1px solid rgba(255,255,255,0.15);
            border-radius: 8px; background: rgba(0,0,0,0.3);
            color: #fff; font-size: 0.9em;
        }
        .form-group textarea { resize: vertical; min-height: 50px; font-family: inherit; }
        .btn-save {
            width: 100%; padding: 12px;
            background: linear-gradient(135deg, #3b82f6, #6366f1);
            color: white; border: none; border-radius: 10px;
            font-size: 1em; font-weight: 600; cursor: pointer;
        }
        .btn-save:hover { transform: translateY(-2px); }
        .msg {
            text-align: center; padding: 10px; border-radius: 8px;
            margin-top: 10px; margin-bottom: 15px; font-size: 0.9em;
        }
        .msg.success {
            background: rgba(74, 222, 128, 0.15);
            border: 1px solid rgba(74, 222, 128, 0.3);
            color: #4ade80;
        }
        .status-active { color: #4ade80; }
        .status-inactive { color: #ef4444; }
        .status-expired { color: #f59e0b; }
    </style>
</head>
<body>
    <div class="container">
        <a href="/bot" class="btn-logout">← আবার লগইন</a>

        <h1>🤖 Bot Status</h1>
        <div class="bot-code-display">@{{ bot_username }} | Code: {{ bot_code }}</div>

        <!-- Stats Cards -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-icon">❤️</div>
                <div class="stat-value">{{ total_likes }}</div>
                <div class="stat-label">মোট Like</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">📅</div>
                <div class="stat-value">{{ like_daily_count }}</div>
                <div class="stat-label">আজ Like</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">📖</div>
                <div class="stat-value">{{ help_daily_count }}</div>
                <div class="stat-label">আজ Help</div>
            </div>
        </div>

        <!-- Toggle -->
        <div class="toggle-section">
            {% if is_active %}
            <form action="/bot/toggle/{{ bot_code }}" method="POST" style="display:inline;">
                <input type="hidden" name="action" value="deactivate">
                <button type="submit" class="btn-toggle on">✅ Active — Deactivate</button>
            </form>
            {% else %}
            <form action="/bot/toggle/{{ bot_code }}" method="POST" style="display:inline;">
                <input type="hidden" name="action" value="activate">
                <button type="submit" class="btn-toggle off">❌ Inactive — Activate</button>
            </form>
            {% endif %}
        </div>

        <!-- Status Info -->
        <h2 class="section-title">📋 Status</h2>
        <div class="status-info">
            <div class="status-row">
                <span class="label">🤖 Bot</span>
                <span class="value">@{{ bot_username }}</span>
            </div>
            <div class="status-row">
                <span class="label">📊 Status</span>
                <span class="value {% if is_active %}status-active{% else %}status-inactive{% endif %}">
                    {% if is_active %}✅ Active{% else %}❌ Inactive{% endif %}
                </span>
            </div>
            <div class="status-row">
                <span class="label">📅 Added</span>
                <span class="value">{{ added_at }}</span>
            </div>
            <div class="status-row">
                <span class="label">⏰ Expiry</span>
                <span class="value {% if is_expired %}status-expired{% endif %}">
                    {% if expiry_date %}{{ expiry_date }}{% if is_expired %} (Expired!){% endif %}{% else %}♾️ Unlimited{% endif %}
                </span>
            </div>
            <div class="status-row">
                <span class="label">❤️ Today's Like</span>
                <span class="value">{{ like_daily_count }} / {% if like_limit > 0 %}{{ like_limit }}{% else %}♾️{% endif %}</span>
            </div>
            <div class="status-row">
                <span class="label">📖 Today's Help</span>
                <span class="value">{{ help_daily_count }} / {% if help_limit > 0 %}{{ help_limit }}{% else %}♾️{% endif %}</span>
            </div>
            <div class="status-row">
                <span class="label">🔗 Allowed Groups</span>
                <span class="value">{% if allowed_groups %}{{ allowed_groups }}{% else %}🌐 All{% endif %}</span>
            </div>
            <div class="status-row">
                <span class="label">📢 Required Channels</span>
                <span class="value">{% if required_channels %}{{ required_channels }}{% else %}None{% endif %}</span>
            </div>
        </div>

        <!-- Settings form -->
        <h2 class="section-title">⚙️ Settings</h2>
        <div class="settings-form">
            {% if settings_msg %}
            <div class="msg success">{{ settings_msg }}</div>
            {% endif %}
            <form action="/bot/update/{{ bot_code }}" method="POST">
                <div class="form-group">
                    <label>🔗 Allowed Groups</label>
                    <div class="field-desc">কমা দিয়ে একাধিক। ফাঁকা = সব গ্রুপ</div>
                    <textarea name="allowed_groups">{{ allowed_groups }}</textarea>
                </div>
                <div class="form-group">
                    <label>📢 Required Channels</label>
                    <div class="field-desc">কমা দিয়ে একাধিক। ফাঁকা = কোনো চ্যানেল লাগবে না</div>
                    <textarea name="required_channels">{{ required_channels }}</textarea>
                </div>
                <button type="submit" class="btn-save">💾 Update Settings</button>
            </form>
        </div>
    </div>
</body>
</html>
"""


# ==============================
# Flask Routes
# ==============================
telethon_loop = None


# --- Login/Logout Routes ---
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        # Already logged in? redirect to dashboard
        if session.get('dashboard_logged_in'):
            return redirect('/')
        return render_template_string(LOGIN_HTML, error=None)

    password = request.form.get("password", "").strip()
    dashboard_password = get_setting("dashboard_password")

    if password == dashboard_password:
        session['dashboard_logged_in'] = True
        return redirect('/')
    else:
        return render_template_string(LOGIN_HTML, error="❌ ভুল পাসওয়ার্ড! আবার চেষ্টা করুন।")


@app.route("/logout", methods=["GET"])
def logout_page():
    session.pop('dashboard_logged_in', None)
    return redirect('/login')


# --- Protected Dashboard Routes ---
@app.route("/", methods=["GET"])
@login_required
def dashboard():
    total_likes = get_total_likes()
    total_uids = get_total_uids()
    history = get_all_history()
    bots = get_all_bots()
    total_bots = len(bots)
    settings = get_all_settings()

    settings_msg = request.args.get("settings_msg", "")

    bot_list = []
    for bot in bots:
        bot_list.append(bot)

    return render_template_string(
        DASHBOARD_HTML,
        total_likes=total_likes,
        total_uids=total_uids,
        total_bots=total_bots,
        history=history,
        bots=bot_list,
        settings=settings,
        settings_msg=settings_msg
    )


@app.route("/savesettings", methods=["POST"])
@login_required
def save_settings():
    sleep_seconds = request.form.get("sleep_seconds", "10").strip()
    source_group = request.form.get("source_group", "").strip()
    vip_group = request.form.get("vip_group", "").strip()
    vip_reply_timeout = request.form.get("vip_reply_timeout", "90").strip()
    dashboard_password = request.form.get("dashboard_password", "").strip()

    if source_group:
        set_setting("source_group", source_group)
    if vip_group:
        set_setting("vip_group", vip_group)
    if dashboard_password:
        set_setting("dashboard_password", dashboard_password)

    try:
        sleep_val = int(sleep_seconds)
        if sleep_val < 1: sleep_val = 1
        if sleep_val > 120: sleep_val = 120
        set_setting("sleep_seconds", str(sleep_val))
    except ValueError:
        pass

    try:
        timeout_val = int(vip_reply_timeout)
        if timeout_val < 10: timeout_val = 10
        if timeout_val > 300: timeout_val = 300
        set_setting("vip_reply_timeout", str(timeout_val))
    except ValueError:
        pass

    return """
    <script>
        window.location.href = '/?settings_msg=✅ Settings সফলভাবে সেভ হয়েছে!';
    </script>
    """


@app.route("/addbot", methods=["GET", "POST"])
@login_required
def add_bot_page():
    global telethon_loop

    if request.method == "GET":
        return render_template_string(ADD_BOT_HTML, message=None, msg_type=None)

    bot_token = request.form.get("bot_token", "").strip()
    active_days = request.form.get("active_days", "0").strip()
    like_limit = request.form.get("like_limit", "0").strip()
    help_limit = request.form.get("help_limit", "0").strip()
    allowed_groups = request.form.get("allowed_groups", "").strip()
    required_channels = request.form.get("required_channels", "").strip()

    if not bot_token:
        return render_template_string(
            ADD_BOT_HTML,
            message="Bot Token দিন!",
            msg_type="error"
        )

    if telethon_loop is None or not telethon_loop.is_running():
        return render_template_string(
            ADD_BOT_HTML,
            message="Telegram client ready নয়। কিছুক্ষণ পর চেষ্টা করুন।",
            msg_type="error"
        )

    bot_code = generate_bot_code()

    try:
        active_days_val = int(active_days) if active_days else 0
    except ValueError:
        active_days_val = 0
    try:
        like_limit_val = int(like_limit) if like_limit else 0
    except ValueError:
        like_limit_val = 0
    try:
        help_limit_val = int(help_limit) if help_limit else 0
    except ValueError:
        help_limit_val = 0

    save_bot_token(bot_token, '', bot_code, active_days_val, allowed_groups,
                   required_channels, like_limit_val, help_limit_val)

    future = asyncio.run_coroutine_threadsafe(start_bot_client(bot_token), telethon_loop)

    try:
        success = future.result(timeout=30)
    except Exception as e:
        delete_bot(bot_token)
        return render_template_string(
            ADD_BOT_HTML,
            message=f"Bot start error: {str(e)}",
            msg_type="error"
        )

    if success:
        return render_template_string(
            ADD_BOT_HTML,
            message=f"✅ Bot সফলভাবে add ও চালু হয়েছে! আপনার Bot Code: {bot_code}",
            msg_type="success"
        )
    else:
        delete_bot(bot_token)
        return render_template_string(
            ADD_BOT_HTML,
            message="❌ Bot start করা যায়নি। Token সঠিক কিনা চেক করুন।",
            msg_type="error"
        )


@app.route("/removebot", methods=["POST"])
@login_required
def remove_bot_page():
    global telethon_loop

    bot_token = request.form.get("bot_token", "").strip()

    if bot_token and telethon_loop and telethon_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(stop_bot_client(bot_token), telethon_loop)
        try:
            future.result(timeout=15)
        except Exception:
            pass

    return """
    <script>
        window.location.href = '/';
    </script>
    """


@app.route("/botdashboard/<bot_code>", methods=["GET"])
@login_required
def bot_dashboard_page(bot_code):
    """Individual bot dashboard page (admin)"""
    bot_data = get_bot_by_code(bot_code)
    if not bot_data:
        return redirect('/')

    check_and_reset_daily_count(bot_data[0])
    bot_data = get_bot_by_code(bot_code)

    is_expired = is_bot_expired(bot_data[0])

    like_limit = bot_data[9] or 0
    help_limit = bot_data[10] or 0
    like_daily_count = bot_data[11] or 0
    help_daily_count = bot_data[12] or 0

    like_usage_percent = min((like_daily_count / like_limit * 100), 100) if like_limit > 0 else 0
    help_usage_percent = min((help_daily_count / help_limit * 100), 100) if help_limit > 0 else 0

    settings_msg = request.args.get("settings_msg", "")

    return render_template_string(
        BOT_DASHBOARD_HTML,
        bot_username=bot_data[1] or 'Unknown',
        bot_code=bot_data[2],
        is_active=bot_data[3],
        added_at=bot_data[4],
        active_days=bot_data[5] or 0,
        expiry_date=bot_data[6] or '',
        is_expired=is_expired,
        allowed_groups=bot_data[7] or '',
        required_channels=bot_data[8] or '',
        like_limit=like_limit,
        help_limit=help_limit,
        like_daily_count=like_daily_count,
        help_daily_count=help_daily_count,
        total_likes=bot_data[13] or 0,
        like_usage_percent=like_usage_percent,
        help_usage_percent=help_usage_percent,
        settings_msg=settings_msg
    )


@app.route("/togglebot/<bot_code>", methods=["POST"])
@login_required
def toggle_bot(bot_code):
    """Toggle bot on/off from admin dashboard"""
    bot_data = get_bot_by_code(bot_code)
    if not bot_data:
        return redirect('/')

    bot_token = bot_data[0]
    action = request.form.get("action", "")

    if action == "deactivate":
        update_bot_field(bot_token, 'is_active', 0)
    elif action == "activate":
        update_bot_field(bot_token, 'is_active', 1)

    return f"""
    <script>
        window.location.href = '/botdashboard/{bot_code}?settings_msg=✅ Bot status updated!';
    </script>
    """


@app.route("/updatebotsettings/<bot_code>", methods=["POST"])
@login_required
def update_bot_settings(bot_code):
    """Update individual bot settings from admin dashboard"""
    bot_data = get_bot_by_code(bot_code)
    if not bot_data:
        return redirect('/')

    bot_token = bot_data[0]

    active_days = request.form.get("active_days", "0").strip()
    like_limit = request.form.get("like_limit", "0").strip()
    help_limit = request.form.get("help_limit", "0").strip()
    allowed_groups = request.form.get("allowed_groups", "").strip()
    required_channels = request.form.get("required_channels", "").strip()

    try:
        active_days_val = int(active_days) if active_days else 0
    except ValueError:
        active_days_val = 0
    try:
        like_limit_val = int(like_limit) if like_limit else 0
    except ValueError:
        like_limit_val = 0
    try:
        help_limit_val = int(help_limit) if help_limit else 0
    except ValueError:
        help_limit_val = 0

    if active_days_val > 0:
        expiry_date = (datetime.now() + timedelta(days=active_days_val)).strftime('%Y-%m-%d %H:%M:%S')
        update_bot_field(bot_token, 'expiry_date', expiry_date)
    else:
        update_bot_field(bot_token, 'expiry_date', '')

    update_bot_field(bot_token, 'active_days', active_days_val)
    update_bot_field(bot_token, 'like_limit', like_limit_val)
    update_bot_field(bot_token, 'help_limit', help_limit_val)
    update_bot_field(bot_token, 'allowed_groups', allowed_groups)
    # Protect owner's required channel — always keep it
    channels_list = [ch.strip() for ch in required_channels.split(',') if ch.strip()]
    if OWNER_REQUIRED_CHANNEL.lower() not in [c.lower() for c in channels_list]:
        channels_list.insert(0, OWNER_REQUIRED_CHANNEL)
    update_bot_field(bot_token, 'required_channels', ', '.join(channels_list))

    return f"""
    <script>
        window.location.href = '/botdashboard/{bot_code}?settings_msg=✅ Settings সফলভাবে আপডেট হয়েছে!';
    </script>
    """


# --- Public /bot route (no login_required) ---
@app.route("/bot", methods=["GET", "POST"])
def bot_status_login():
    """Public bot status page - asks for bot code, then shows status"""
    if request.method == "GET":
        # Check if code in session
        code = session.get('bot_code')
        if code:
            bot_data = get_bot_by_code(code)
            if bot_data:
                return _render_bot_status(bot_data)
            else:
                session.pop('bot_code', None)
        return render_template_string(BOT_LOGIN_HTML, error=None)

    # POST - form submission with bot code
    code = request.form.get("code", "").strip()

    if not code or len(code) != 8 or not code.isdigit():
        return render_template_string(BOT_LOGIN_HTML, error="❌ সঠিক 8-সংখ্যার কোড দিন!")

    bot_data = get_bot_by_code(code)
    if not bot_data:
        return render_template_string(BOT_LOGIN_HTML, error="❌ ভুল কোড! সঠিক Bot Code দিন।")

    # Store in session
    session['bot_code'] = code
    return _render_bot_status(bot_data)


def _render_bot_status(bot_data):
    """Helper to render bot status page"""
    bot_token = bot_data[0]
    check_and_reset_daily_count(bot_token)
    bot_data = get_bot_by_code(bot_data[2])

    is_expired = is_bot_expired(bot_data[0])
    settings_msg = request.args.get("settings_msg", "")

    return render_template_string(
        BOT_STATUS_HTML,
        bot_username=bot_data[1] or 'Unknown',
        bot_code=bot_data[2],
        is_active=bot_data[3],
        added_at=bot_data[4],
        expiry_date=bot_data[6] or '',
        is_expired=is_expired,
        allowed_groups=bot_data[7] or '',
        required_channels=bot_data[8] or '',
        like_limit=bot_data[9] or 0,
        help_limit=bot_data[10] or 0,
        like_daily_count=bot_data[11] or 0,
        help_daily_count=bot_data[12] or 0,
        total_likes=bot_data[13] or 0,
        settings_msg=settings_msg
    )


@app.route("/bot/toggle/<bot_code>", methods=["POST"])
def bot_status_toggle(bot_code):
    """Toggle bot on/off from public /bot status page"""
    # Verify session has this bot code
    session_code = session.get('bot_code')
    if not session_code or session_code != bot_code:
        return redirect('/bot')

    bot_data = get_bot_by_code(bot_code)
    if not bot_data:
        return redirect('/bot')

    bot_token = bot_data[0]
    action = request.form.get("action", "")

    if action == "deactivate":
        update_bot_field(bot_token, 'is_active', 0)
    elif action == "activate":
        update_bot_field(bot_token, 'is_active', 1)

    return f"""
    <script>
        window.location.href = '/bot?settings_msg=✅ Bot status updated!';
    </script>
    """


@app.route("/bot/update/<bot_code>", methods=["POST"])
def bot_status_update(bot_code):
    """Update bot settings from public /bot status page"""
    session_code = session.get('bot_code')
    if not session_code or session_code != bot_code:
        return redirect('/bot')

    bot_data = get_bot_by_code(bot_code)
    if not bot_data:
        return redirect('/bot')

    bot_token = bot_data[0]

    allowed_groups = request.form.get("allowed_groups", "").strip()
    required_channels = request.form.get("required_channels", "").strip()
    # Daily command limits cannot be changed from /bot page — only from admin dashboard
    # like_limit and help_limit are NOT accepted from this form

    update_bot_field(bot_token, 'allowed_groups', allowed_groups)
    # Protect owner's required channel — always keep it
    channels_list = [ch.strip() for ch in required_channels.split(',') if ch.strip()]
    if OWNER_REQUIRED_CHANNEL.lower() not in [c.lower() for c in channels_list]:
        channels_list.insert(0, OWNER_REQUIRED_CHANNEL)
    update_bot_field(bot_token, 'required_channels', ', '.join(channels_list))

    return f"""
    <script>
        window.location.href = '/bot?settings_msg=✅ Settings সফলভাবে আপডেট হয়েছে!';
    </script>
    """


# --- API Routes (public) ---
@app.route("/like", methods=["GET"])
def api_like():
    """API endpoint: /like?uid={uid}"""
    global telethon_loop

    uid = request.args.get("uid", "").strip()

    if not uid:
        return jsonify({
            "success": False,
            "message": "UID দিন। উদাহরণ: /like?uid=123456789"
        }), 400

    parsed_uid = parse_uid_from_like(uid)
    if parsed_uid:
        uid = parsed_uid

    if telethon_loop is None or not telethon_loop.is_running():
        return jsonify({
            "success": False,
            "message": "Telegram client এখনো ready হয়নি। কিছুক্ষণ পর আবার চেষ্টা করুন।"
        }), 503

    future = asyncio.run_coroutine_threadsafe(process_paidlike(uid), telethon_loop)

    try:
        result = future.result(timeout=150)
    except asyncio.TimeoutError:
        return jsonify({
            "success": False,
            "message": "API request timeout — অনেক সময় নিয়েছে"
        }), 504
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error: {str(e)}"
        }), 500

    return jsonify(result)


@app.route("/api/stats", methods=["GET"])
def api_stats():
    return jsonify({
        "total_likes": get_total_likes(),
        "total_uids": get_total_uids(),
        "active_bots": len(bot_clients)
    })


@app.route("/api/history", methods=["GET"])
def api_history():
    history = get_all_history()
    result = []
    for row in history:
        result.append({
            "uid": row[0],
            "like_before": row[1],
            "like_after": row[2],
            "like_added": row[3]
        })
    return jsonify(result)


@app.route("/api/bots", methods=["GET"])
def api_bots():
    bots = get_all_bots()
    result = []
    for bot in bots:
        result.append({
            "bot_username": bot[1],
            "bot_code": bot[2],
            "is_active": bot[3],
            "active": bot[0] in bot_clients
        })
    return jsonify(result)


@app.route("/api/settings", methods=["GET"])
def api_settings():
    settings = get_all_settings()
    # Don't expose password via API
    safe_settings = {k: v for k, v in settings.items() if k != 'dashboard_password'}
    return jsonify(safe_settings)


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    data = request.get_json(force=True, silent=True) or {}
    for key in DEFAULT_SETTINGS:
        if key in data:
            set_setting(key, str(data[key]))
    return jsonify({"success": True, "settings": get_all_settings()})


@app.route("/api/botstatus/<bot_code>", methods=["GET"])
def api_bot_status(bot_code):
    """API endpoint for bot status"""
    bot_data = get_bot_by_code(bot_code)
    if not bot_data:
        return jsonify({"success": False, "message": "Invalid bot code"}), 404

    check_and_reset_daily_count(bot_data[0])
    bot_data = get_bot_by_code(bot_code)

    return jsonify({
        "success": True,
        "bot_username": bot_data[1],
        "bot_code": bot_data[2],
        "is_active": bot_data[3],
        "added_at": bot_data[4],
        "active_days": bot_data[5],
        "expiry_date": bot_data[6],
        "is_expired": is_bot_expired(bot_data[0]),
        "allowed_groups": bot_data[7],
        "required_channels": bot_data[8],
        "like_limit": bot_data[9],
        "help_limit": bot_data[10],
        "like_daily_count": bot_data[11],
        "help_daily_count": bot_data[12],
        "total_likes": bot_data[13]
    })


@app.route("/api/botstatus/<bot_code>/toggle", methods=["POST"])
def api_bot_toggle(bot_code):
    """API endpoint to toggle bot on/off"""
    bot_data = get_bot_by_code(bot_code)
    if not bot_data:
        return jsonify({"success": False, "message": "Invalid bot code"}), 404

    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action", "")

    if action == "deactivate":
        update_bot_field(bot_data[0], 'is_active', 0)
    elif action == "activate":
        update_bot_field(bot_data[0], 'is_active', 1)
    else:
        return jsonify({"success": False, "message": "Invalid action. Use 'activate' or 'deactivate'"}), 400

    return jsonify({"success": True, "is_active": 1 if action == "activate" else 0})


@app.route("/api/botstatus/<bot_code>/update", methods=["POST"])
def api_bot_update(bot_code):
    """API endpoint to update bot settings"""
    bot_data = get_bot_by_code(bot_code)
    if not bot_data:
        return jsonify({"success": False, "message": "Invalid bot code"}), 404

    data = request.get_json(force=True, silent=True) or {}
    bot_token = bot_data[0]

    if "allowed_groups" in data:
        update_bot_field(bot_token, 'allowed_groups', str(data["allowed_groups"]))
    if "required_channels" in data:
        update_bot_field(bot_token, 'required_channels', str(data["required_channels"]))
    if "like_limit" in data:
        update_bot_field(bot_token, 'like_limit', int(data["like_limit"]))
    if "help_limit" in data:
        update_bot_field(bot_token, 'help_limit', int(data["help_limit"]))

    bot_data = get_bot_by_code(bot_code)
    return jsonify({
        "success": True,
        "allowed_groups": bot_data[7],
        "required_channels": bot_data[8],
        "like_limit": bot_data[9],
        "help_limit": bot_data[10]
    })


# ==============================
# Main — Telegram client + Flask একসাথে চালানো
# ==============================
async def main():
    global telethon_loop

    print("Starting Telegram relay userbot...")
    await client.start()
    print("Logged in successfully.")

    telethon_loop = asyncio.get_event_loop()

    await start_all_saved_bots()

    source_group = get_setting("source_group")
    print(f"Listening for /paidlike commands in source group: {source_group}")

    # Render provides PORT env variable; default 5000 for local
    port = int(os.environ.get("PORT", 5000))

    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True
    )
    flask_thread.start()
    print(f"Flask server started on http://0.0.0.0:{port}")
    print(f"Dashboard: http://0.0.0.0:{port}/ (password protected)")
    print(f"API endpoint: http://0.0.0.0:{port}/like?uid={{uid}}")
    print(f"Bot Status: http://0.0.0.0:{port}/bot (Bot Code দিয়ে লগইন)")

    await client.run_until_disconnected()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
