import os
import random
import time
import pytz
from datetime import datetime, timedelta
import logging
from threading import Thread
from flask import Flask, request, jsonify
import telebot
import psycopg2
from psycopg2 import sql
from urllib.parse import urlparse
from contextlib import contextmanager

# ================= INITIAL SETUP =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================= CONFIGURATION =================
# Environment variables with defaults for local testing
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME', 'testsub01')
BOT_USERNAME = os.getenv('BOT_USERNAME')
COOLDOWN_SECONDS = int(os.getenv('COOLDOWN_SECONDS', 120))
PREDICTION_DELAY = int(os.getenv('PREDICTION_DELAY', 130))
SHARES_REQUIRED = int(os.getenv('SHARES_REQUIRED', 1))
INDIAN_TIMEZONE = pytz.timezone('Asia/Kolkata')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
SERVER_URL = os.getenv('SERVER_URL', 'https://telegram-live.onrender.com')
WEBHOOK_PORT = int(os.getenv('PORT', 8080))
UPTIME_ROBOT_URL = os.getenv('UPTIME_ROBOT_URL')  # For keeping the bot awake

# Emojis
ROCKET = "🚀"
LOCK = "🔒"
CHECK = "✅"
CROSS = "❌"
HOURGLASS = "⏳"
DIAMOND = "◆"
GRAPH = "📈"
SHIELD = "🛡️"
ROCKET_STICKER_ID = "CAACAgUAAxkBAAEL3xRmEeX3xQABHYYYr4YH1LQhUe3VdW8AAp4LAAIWjvlVjXjWbJQN0k80BA"

# Initialize bot
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Trackers
first_time_users = set()
cooldowns = {}
membership_cache = {}  # Cache for membership status to reduce DB queries
referral_cache = {}    # Cache for referral counts

# ================= DATABASE UTILITIES =================
@contextmanager
def db_connection():
    """Context manager for database connections to ensure proper cleanup"""
    conn = None
    try:
        # Parse the database URL if using Render's internal database URL
        db_url = os.getenv('DATABASE_URL')
        if db_url:
            result = urlparse(db_url)
            conn = psycopg2.connect(
                database=result.path[1:],
                user=result.username,
                password=result.password,
                host=result.hostname,
                port=result.port
            )
        else:
            # For local testing
            conn = psycopg2.connect(
                dbname=os.getenv('DB_NAME', 'telegram_bot'),
                user=os.getenv('DB_USER', 'postgres'),
                password=os.getenv('DB_PASSWORD', ''),
                host=os.getenv('DB_HOST', 'localhost')
            )
        yield conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise
    finally:
        if conn:
            conn.close()

@contextmanager
def db_cursor():
    """Context manager for database cursor"""
    with db_connection() as conn:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            cur.close()

def initialize_database():
    """Create necessary tables if they don't exist"""
    commands = (
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username VARCHAR(255),
            first_name VARCHAR(255),
            last_name VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id SERIAL PRIMARY KEY,
            referrer_id BIGINT NOT NULL,
            referred_id BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(referrer_id, referred_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS live_requests (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL UNIQUE
        )
        """
    )
    
    try:
        with db_cursor() as cur:
            for command in commands:
                cur.execute(command)
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        raise

# ================= CACHED DATABASE FUNCTIONS =================
def get_admins():
    """Get list of admin user IDs with caching"""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT user_id FROM admins")
            admins = [str(row[0]) for row in cur.fetchall()]
            return admins
    except Exception as e:
        logger.error(f"Error getting admins: {e}")
        return []

def is_admin(user_id):
    """Check if user is admin"""
    return str(user_id) in get_admins()

def is_member(user_id):
    """Check if user is member of channel with caching"""
    if user_id in membership_cache:
        return membership_cache[user_id]
    
    try:
        member = bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        is_member = member.status in ["member", "administrator", "creator"]
        membership_cache[user_id] = is_member  # Cache the result
        return is_member
    except Exception as e:
        logger.error(f"Membership check error: {e}")
        return False

def count_user_referrals(user_id):
    """Count how many referrals a user has with caching"""
    if user_id in referral_cache:
        return referral_cache[user_id]
    
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = %s",
                (user_id,)
            )
            count = cur.fetchone()[0]
            referral_cache[user_id] = count  # Cache the result
            return count
    except Exception as e:
        logger.error(f"Error counting referrals: {e}")
        return 0

def has_shared_enough(user_id):
    """Check if user has enough referrals"""
    return count_user_referrals(user_id) >= SHARES_REQUIRED

def save_live_request(user_id):
    """Save a live prediction request"""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO live_requests (user_id) 
                VALUES (%s)
                ON CONFLICT (user_id) DO NOTHING
                RETURNING id
                """,
                (user_id,)
            )
            # If a row was inserted, cur.fetchone() will return the id
            # If there was a conflict, it will return None
            return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"Error saving live request: {e}")
        return False

def count_live_requests():
    """Count total live requests"""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM live_requests")
            return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"Error counting live requests: {e}")
        return 0

def clear_live_requests():
    """Clear all live requests"""
    try:
        with db_cursor() as cur:
            cur.execute("TRUNCATE live_requests")
            return True
    except Exception as e:
        logger.error(f"Error clearing live requests: {e}")
        return False

def save_user_if_eligible(user_info):
    """Save user to database only if they meet all requirements"""
    user_id = user_info.id
    
    # Clear cache for this user since we're updating their status
    membership_cache.pop(user_id, None)
    referral_cache.pop(user_id, None)
    
    try:
        with db_cursor() as cur:
            # Check if user exists first
            cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
            if cur.fetchone():
                return True  # User already exists
                
            # Only save if they meet requirements
            if is_member(user_id) and (SHARES_REQUIRED == 0 or has_shared_enough(user_id)):
                cur.execute(
                    """
                    INSERT INTO users (user_id, username, first_name, last_name)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (user_info.id, user_info.username, user_info.first_name, user_info.last_name)
                )
                # Clear referral cache for anyone who referred this user
                cur.execute("SELECT referrer_id FROM referrals WHERE referred_id = %s", (user_id,))
                for row in cur.fetchall():
                    referral_cache.pop(row[0], None)
                return True
            return False
    except Exception as e:
        logger.error(f"Error saving eligible user: {e}")
        return False

def save_referral(referrer_id, referred_id):
    """Save referral relationship and clear cache"""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO referrals (referrer_id, referred_id)
                VALUES (%s, %s)
                ON CONFLICT (referrer_id, referred_id) DO NOTHING
                """,
                (referrer_id, referred_id)
            )
            # Clear referral cache for referrer since count may have changed
            referral_cache.pop(referrer_id, None)
            return True
    except Exception as e:
        logger.error(f"Error saving referral: {e}")
        return False

def get_live_requests():
    """Get all live prediction requests"""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT user_id FROM live_requests")
            return [str(row[0]) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Error getting live requests: {e}")
        return []

def get_users():
    """Get all users from database"""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT user_id, username, first_name, last_name FROM users")
            return [{
                'user_id': str(row[0]),
                'username': row[1],
                'first_name': row[2],
                'last_name': row[3]
            } for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Error getting users: {e}")
        return []

# ================= UTILITY FUNCTIONS =================

def send_batch_messages(bot, user_ids, send_func, *args, **kwargs):
    """Send messages in batches to avoid rate limits"""
    BATCH_SIZE = 30  # Telegram's limit is about 30 messages per second
    delay_between_batches = 1  # 1 second delay between batches
    
    success = 0
    failures = 0
    
    for i in range(0, len(user_ids), BATCH_SIZE):
        batch = user_ids[i:i+BATCH_SIZE]
        for user_id in batch:
            try:
                send_func(user_id, *args, **kwargs)
                success += 1
            except Exception as e:
                logger.error(f"Failed to send to {user_id}: {e}")
                failures += 1
        time.sleep(delay_between_batches)
    
    return success, failures

def get_indian_time():
    """Get current time in Indian timezone"""
    return datetime.now(INDIAN_TIMEZONE)

def format_time(dt):
    """Format time as HH:MM"""
    return dt.strftime("%H:%M")

def generate_prediction():
    """Generate a random prediction"""
    pred = round(random.uniform(2.50, 4.50), 2)
    safe = round(random.uniform(1.50, min(pred, 3.0)), 2)
    future_time = get_indian_time() + timedelta(seconds=PREDICTION_DELAY)
    return format_time(future_time), pred, safe

def get_share_markup(user_id):
    """Create markup for sharing the bot"""
    markup = telebot.types.InlineKeyboardMarkup()
    share_btn = telebot.types.InlineKeyboardButton(
        f"{ROCKET} Share Bot {ROCKET}",
        url=f"https://t.me/share/url?url=t.me/{BOT_USERNAME}?start={user_id}&text=Check%20out%20this%20awesome%20prediction%20bot!"
    )
    markup.add(share_btn)
    markup.add(telebot.types.InlineKeyboardButton("✅ Verify Shares", callback_data="verify_shares"))
    return markup

def get_main_markup(user_id):
    """Create main menu markup"""
    markup = telebot.types.InlineKeyboardMarkup()
    if is_member(user_id) and has_shared_enough(user_id):
        markup.row(
            telebot.types.InlineKeyboardButton(f"{ROCKET} Generate Prediction", callback_data="get_prediction"),
            telebot.types.InlineKeyboardButton(f"📡 Request Live Prediction", callback_data="request_live")
        )
    return markup

def get_admin_markup():
    """Create admin panel markup"""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("📊 Check Requests", callback_data="check_requests"),
        telebot.types.InlineKeyboardButton("🧹 Clear Requests", callback_data="clear_requests")
    )
    markup.row(
        telebot.types.InlineKeyboardButton("📤 Send Message", callback_data="send_prediction"),
        telebot.types.InlineKeyboardButton("👥 Check Users", callback_data="check_users")
    )

    markup.row(
        telebot.types.InlineKeyboardButton("🌀 Reset Users to Start", callback_data="batch_reset_users")
    )

    
    return markup

def notify_admins(message):
    """Notify all admins"""
    for admin_id in get_admins():
        try:
            bot.send_message(admin_id, message)
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

def safe_int_convert(value, default=0):
    """Safely convert to integer with default fallback"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def ping_uptime_robot():
    """Ping UptimeRobot to keep the bot awake"""
    if UPTIME_ROBOT_URL:
        try:
            import requests
            requests.get(UPTIME_ROBOT_URL)
            logger.info("Pinged UptimeRobot to keep the bot awake")
        except Exception as e:
            logger.error(f"Error pinging UptimeRobot: {e}")

# ================= BOT HANDLERS =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    try:
        user_id = message.chat.id
        user_info = message.from_user
        
        # Clear cache for this user since we're checking fresh status
        membership_cache.pop(user_id, None)
        referral_cache.pop(user_id, None)
        
        # Process referral only if user joined channel AND is not already registered
        if len(message.text.split()) > 1 and is_member(user_id):
            try:
                referrer_str = message.text.split()[1]
                referrer_id = safe_int_convert(referrer_str)
                if referrer_id != 0 and referrer_id != user_id:
                    # Check if user is already registered
                    with db_cursor() as cur:
                        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
                        if not cur.fetchone():  # Only proceed if user NOT in database
                            if save_referral(referrer_id, user_id):
                                logger.info(f"New verified referral: {referrer_id} -> {user_id} (new channel member)")
                                # Clear caches
                                referral_cache.pop(referrer_id, None)
                                membership_cache.pop(user_id, None)
                            else:
                                logger.info(f"Referral already exists: {referrer_id} -> {user_id}")
                        else:
                            logger.info(f"User {user_id} already registered - referral not counted")
            except Exception as e:
                logger.error(f"Referral processing error: {e}")

        # Save user only if eligible
        save_user_if_eligible(user_info)
        
        welcome_msg = (
            f"{GRAPH} *WELCOME TO AI-POWERED PREDICTION BOT* {GRAPH}\n\n"
            f"{DIAMOND} Use suggested assurance for risk management\n"
            f"{DIAMOND} Follow cooldown periods\n\n"
            f"{SHIELD} *VIP Channel:* @{CHANNEL_USERNAME}"
        )
        
        if is_member(user_id) and (SHARES_REQUIRED == 0 or has_shared_enough(user_id)):
            bot.send_message(user_id, welcome_msg, reply_markup=get_main_markup(user_id), parse_mode="Markdown")
        elif not is_member(user_id):
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(
                telebot.types.InlineKeyboardButton("Join VIP Channel", url=f"https://t.me/{CHANNEL_USERNAME}"),
                telebot.types.InlineKeyboardButton("Verify Membership", callback_data="check_membership")
            )
            bot.send_message(user_id, f"{CROSS} *PREMIUM ACCESS REQUIRED*\n\nJoin @{CHANNEL_USERNAME} then verify.", 
                           reply_markup=markup, parse_mode="Markdown")
        else:
            shares_count = count_user_referrals(user_id)
            share_msg = (
                f"{LOCK} *SHARE REQUIREMENT*\n\n"
                f"Refer {SHARES_REQUIRED} friend{'s' if SHARES_REQUIRED > 1 else ''} (who join channel) to unlock.\n"
                f"Current valid referrals: {shares_count}/{SHARES_REQUIRED}\n\n"
                "How to refer:\n"
                "1. Click 'Share Bot' below\n"
                "2. Send to friends\n"
                "3. They must JOIN CHANNEL and START bot\n"
                "4. Verify after they join"
            )
            bot.send_message(user_id, share_msg, reply_markup=get_share_markup(user_id), parse_mode="Markdown")
            
    except Exception as e:
        logger.error(f"Welcome error: {e}")
        bot.send_message(message.chat.id, "⚠️ An error occurred. Please try again.")
        
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    try:
        user_id = message.chat.id
        if is_admin(user_id):
            bot.send_message(user_id, "🛠 *Admin Panel* 🛠", reply_markup=get_admin_markup(), parse_mode="Markdown")
        else:
            bot.send_message(user_id, "⛔ Unauthorized access!")
    except Exception as e:
        logger.error(f"Admin panel error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "check_membership")
def check_membership(call):
    try:
        user_id = call.message.chat.id
        # Clear cache to force fresh check
        membership_cache.pop(user_id, None)
        referral_cache.pop(user_id, None)
        
        if is_member(user_id):
            if has_shared_enough(user_id):
                bot.answer_callback_query(call.id, "✅ Fully verified! You can now get predictions.")
                send_welcome(call.message)
            else:
                shares_needed = SHARES_REQUIRED - count_user_referrals(user_id)
                bot.answer_callback_query(
                    call.id, 
                    f"✅ Membership verified! Need {shares_needed} more referrals.", 
                    show_alert=True
                )
                send_welcome(call.message)
        else:
            bot.answer_callback_query(call.id, "❌ Join channel first!", show_alert=True)
    except Exception as e:
        logger.error(f"Membership check error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "verify_shares")
def verify_shares(call):
    try:
        user_id = call.message.chat.id
        # Clear cache to force fresh check
        membership_cache.pop(user_id, None)
        referral_cache.pop(user_id, None)
        
        if not is_member(user_id):
            bot.answer_callback_query(call.id, "❌ Join channel first, then verify!", show_alert=True)
            return
            
        # Save user if they now meet requirements
        if has_shared_enough(user_id):
            save_user_if_eligible(call.from_user)
            
        shares_count = count_user_referrals(user_id)
        if shares_count >= SHARES_REQUIRED:
            bot.answer_callback_query(call.id, "✅ Fully verified! You can now get predictions.")
            send_welcome(call.message)
        else:
            needed = SHARES_REQUIRED - shares_count
            bot.answer_callback_query(call.id, 
                f"❌ Need {needed} more valid referrals (users who joined channel)", 
                show_alert=True)
    except Exception as e:
        logger.error(f"Share verify error: {e}")
        bot.answer_callback_query(call.id, "⚠️ Error verifying shares. Please try again.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "get_prediction")
def handle_prediction(call):
    try:
        user_id = call.message.chat.id
        
        if not is_member(user_id):
            bot.answer_callback_query(call.id, "❌ Join channel first!", show_alert=True)
            return
            
        if not has_shared_enough(user_id):
            bot.answer_callback_query(call.id, "❌ Complete sharing first!", show_alert=True)
            return
            
        if user_id in cooldowns and (remaining := cooldowns[user_id] - time.time()) > 0:
            mins, secs = divmod(int(remaining), 60)
            bot.answer_callback_query(call.id, f"{LOCK} Wait {mins}m {secs}s", show_alert=True)
            return

        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass

        if user_id not in first_time_users:
            try:
                bot.send_sticker(user_id, ROCKET_STICKER_ID)
                first_time_users.add(user_id)
            except:
                pass

        future_time, pred, safe = generate_prediction()
        prediction_msg = (
            f"{ROCKET} *LUCKY JET PREDICTION*\n"
            "┏━━━━━━━━━━━━━\n"
            f"┠ {DIAMOND} 🕒 Time: {future_time}\n"
            f"┠ {DIAMOND} Coefficient: {pred}X {ROCKET}\n"
            f"┠ {DIAMOND} Assurance: {safe}X\n"
            "┗━━━━━━━━━━━━━\n\n"
            f"{HOURGLASS} Next in {COOLDOWN_SECONDS//60} minutes"
        )
        
        bot.send_message(user_id, prediction_msg, reply_markup=get_main_markup(user_id), parse_mode="Markdown")
        cooldowns[user_id] = time.time() + COOLDOWN_SECONDS
        bot.answer_callback_query(call.id, "✅ Prediction generated!")
        
    except Exception as e:
        logger.error(f"Prediction error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "request_live")
def request_live_prediction(call):
    try:
        user_id = call.message.chat.id
        
        if not is_member(user_id):
            bot.answer_callback_query(call.id, "❌ Join channel first!", show_alert=True)
            return
            
        if not has_shared_enough(user_id):
            bot.answer_callback_query(call.id, "❌ Complete sharing first!", show_alert=True)
            return
            
        if save_live_request(user_id):
            total_requests = count_live_requests()
            bot.answer_callback_query(
                call.id, 
                f"✅ Your request sent, admin will be notified\n{total_requests} members have requested", 
                show_alert=True
            )
            notify_admins(f"👋 Hello admin! Live prediction request received from user {user_id}")
        else:
            bot.answer_callback_query(call.id, "❌ You already have a pending request!", show_alert=True)
            
    except Exception as e:
        logger.error(f"Live prediction request error: {e}")

# ================= ADMIN MESSAGE HANDLERS =================
@bot.callback_query_handler(func=lambda call: call.data == "send_prediction")
def send_prediction_menu(call):
    try:
        user_id = call.message.chat.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(
            telebot.types.InlineKeyboardButton("📝 Text Message", callback_data="send_text"),
            telebot.types.InlineKeyboardButton("🖼️ Image", callback_data="send_image")
        )
        markup.row(
            telebot.types.InlineKeyboardButton("🎵 Voice Message", callback_data="send_voice"),
            telebot.types.InlineKeyboardButton("😄 Sticker", callback_data="send_sticker")
        )
        markup.row(
            telebot.types.InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin")
        )
        
        bot.edit_message_text(
            "📤 Select message type to send:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Send prediction menu error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "back_to_admin")
def back_to_admin(call):
    try:
        user_id = call.message.chat.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        bot.edit_message_text(
            "🛠 *Admin Panel* 🛠",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_admin_markup(),
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Back to admin error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "send_text")
def ask_for_text_message(call):
    try:
        user_id = call.message.chat.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        msg = bot.send_message(user_id, "✍️ Enter the text message to send to verified users:")
        bot.register_next_step_handler(msg, process_text_message)
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ask for text message error: {e}")

def process_text_message(message):
    try:
        if not is_admin(message.chat.id):
            return
            
        text_content = message.text
        verified_users = get_users()
        
        # Filter users who are eligible to receive messages
        eligible_users = []
        for user in verified_users:
            user_id = int(user['user_id'])
            if is_member(user_id) and has_shared_enough(user_id):
                eligible_users.append(user_id)
        
        # Send in batches
        success, failures = send_batch_messages(
            bot,
            eligible_users,
            lambda uid: bot.send_message(uid, f"🟢 *LIVE PREDICTION*\n\n{text_content}", parse_mode="Markdown")
        )
                
        bot.send_message(message.chat.id, f"✅ Text sent to {success} users\n❌ Failed for {failures} users")
        
    except Exception as e:
        logger.error(f"Text message processing error: {e}")
        bot.send_message(message.chat.id, f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "send_image")
def ask_for_image(call):
    try:
        user_id = call.message.chat.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        msg = bot.send_message(user_id, "🖼️ Send the image you want to broadcast (send as photo):")
        bot.register_next_step_handler(msg, process_image_message)
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ask for image error: {e}")

def process_image_message(message):
    try:
        if not is_admin(message.chat.id):
            return
            
        if not message.photo:
            bot.send_message(message.chat.id, "❌ Please send an image as a photo.")
            return
            
        photo = message.photo[-1].file_id
        caption = message.caption if message.caption else "📡 *LIVE PREDICTION*"
        
        verified_users = get_users()
        
        eligible_users = []
        for user in verified_users:
            user_id = int(user['user_id'])
            if is_member(user_id) and has_shared_enough(user_id):
                eligible_users.append(user_id)
        
        success, failures = send_batch_messages(
            bot,
            eligible_users,
            lambda uid: bot.send_photo(uid, photo, caption=caption, parse_mode="Markdown")
        )
                
        bot.send_message(message.chat.id, f"✅ Image sent to {success} users\n❌ Failed for {failures} users")
        
    except Exception as e:
        logger.error(f"Image processing error: {e}")
        bot.send_message(message.chat.id, f"❌ Error: {e}")
@bot.callback_query_handler(func=lambda call: call.data == "send_voice")
def ask_for_voice(call):
    try:
        user_id = call.message.chat.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        msg = bot.send_message(user_id, "🎤 Send the voice message you want to broadcast:")
        bot.register_next_step_handler(msg, process_voice_message)
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ask for voice error: {e}")

def process_voice_message(message):
    try:
        if not is_admin(message.chat.id):
            return
            
        if not message.voice:
            bot.send_message(message.chat.id, "❌ Please send a voice message.")
            return
            
        voice = message.voice.file_id
        caption = message.caption if message.caption else "📡 *LIVE PREDICTION*"
        
        verified_users = get_users()
        
        eligible_users = []
        for user in verified_users:
            user_id = int(user['user_id'])
            if is_member(user_id) and has_shared_enough(user_id):
                eligible_users.append(user_id)
        
        success, failures = send_batch_messages(
            bot,
            eligible_users,
            lambda uid: bot.send_voice(uid, voice, caption=caption, parse_mode="Markdown")
        )
                
        bot.send_message(message.chat.id, f"✅ Voice message sent to {success} users\n❌ Failed for {failures} users")
        
    except Exception as e:
        logger.error(f"Voice processing error: {e}")
        bot.send_message(message.chat.id, f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "send_sticker")
def ask_for_sticker(call):
    try:
        user_id = call.message.chat.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        msg = bot.send_message(user_id, "😄 Send the sticker you want to broadcast:")
        bot.register_next_step_handler(msg, process_sticker_message)
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ask for sticker error: {e}")

def process_sticker_message(message):
    try:
        if not is_admin(message.chat.id):
            return
            
        if not message.sticker:
            bot.send_message(message.chat.id, "❌ Please send a sticker.")
            return
            
        sticker = message.sticker.file_id
        
        verified_users = get_users()
        
        eligible_users = []
        for user in verified_users:
            user_id = int(user['user_id'])
            if is_member(user_id) and has_shared_enough(user_id):
                eligible_users.append(user_id)
        
        success, failures = send_batch_messages(
            bot,
            eligible_users,
            lambda uid: bot.send_sticker(uid, sticker)
        )
                
        bot.send_message(message.chat.id, f"✅ Sticker sent to {success} users\n❌ Failed for {failures} users")
        
    except Exception as e:
        logger.error(f"Sticker processing error: {e}")
        bot.send_message(message.chat.id, f"❌ Error: {e}")
        
@bot.callback_query_handler(func=lambda call: call.data in ["check_requests", "clear_requests", "check_users"])
def admin_actions(call):
    try:
        user_id = call.message.chat.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        if call.data == "check_requests":
            requests = get_live_requests()
            if not requests:
                msg = "📊 No live prediction requests pending."
            else:
                msg = f"📊 Pending Live Requests: {len(requests)}\n\n"
                msg += "\n".join(f"• User ID: {req}" for req in requests[:10])
                if len(requests) > 10:
                    msg += f"\n\n...and {len(requests)-10} more"
            bot.send_message(user_id, msg)
            bot.answer_callback_query(call.id)
            
        elif call.data == "clear_requests":
            if clear_live_requests():
                bot.answer_callback_query(call.id, "✅ All requests cleared!")
            else:
                bot.answer_callback_query(call.id, "❌ Failed to clear requests!")
                
        elif call.data == "check_users":
            users = get_users()
            if not users:
                msg = "👥 No users found in database."
            else:
                msg = f"👥 Total Users: {len(users)}\n\n"
                msg += "\n".join(
                    f"{idx+1}. ID: {user['user_id']} | @{user['username'] if user['username'] else ''} {user['first_name'] or ''} {user['last_name'] or ''}"
                    for idx, user in enumerate(users[:10])
                )
                if len(users) > 10:
                    msg += f"\n\n...and {len(users)-10} more"
            bot.send_message(user_id, msg)
            bot.answer_callback_query(call.id)
            
    except Exception as e:
        logger.error(f"Admin action error: {e}")

# Add batch reset handler
@bot.callback_query_handler(func=lambda call: call.data == "batch_reset_users")
def batch_reset_users(call):
    try:
        user_id = call.message.chat.id
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return

        # Clear runtime states
        global cooldowns, first_time_users, membership_cache, referral_cache
        cooldowns = {}
        first_time_users = set()
        membership_cache = {}
        referral_cache = {}

        # Get all users from users table
        with db_cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            user_ids = [str(row[0]) for row in cur.fetchall()]

        if not user_ids:
            bot.answer_callback_query(call.id, "❌ No users found!", show_alert=True)
            return

        # Send progress message
        progress_msg = bot.send_message(user_id, 
                                     f"🌀 Resetting {len(user_ids)} users...\n"
                                     f"0/{len(user_ids)} completed")

        # Batch settings
        BATCH_SIZE = 25  # Telegram allows ~30 messages/sec
        DELAY_BETWEEN_BATCHES = 1  # 1 second delay
        completed = 0
        failed = 0

        # Process in batches
        for i in range(0, len(user_ids), BATCH_SIZE):
            batch = user_ids[i:i+BATCH_SIZE]
            
            for uid in batch:
                try:
                    bot.send_message(uid, "The bot has been updated. Please click /start to continue.")
                    completed += 1
                except Exception as e:
                    logger.error(f"Failed to reset user {uid}: {e}")
                    failed += 1
                    continue
            
            # Update progress
            try:
                bot.edit_message_text(
                    f"🌀 Resetting {len(user_ids)} users...\n"
                    f"{completed}/{len(user_ids)} completed\n"
                    f"Failed: {failed}",
                    chat_id=progress_msg.chat.id,
                    message_id=progress_msg.message_id
                )
            except:
                pass
            
            # Respect API limits
            if i + BATCH_SIZE < len(user_ids):
                time.sleep(DELAY_BETWEEN_BATCHES)

        # Final update
        bot.edit_message_text(
            f"✅ Reset complete!\n"
            f"Total: {len(user_ids)}\n"
            f"Success: {completed}\n"
            f"Failed: {failed}",
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id
        )
        bot.answer_callback_query(call.id)

    except Exception as e:
        logger.error(f"Batch reset error: {e}")
        bot.answer_callback_query(call.id, "❌ Reset failed!", show_alert=True)
# ================= WEBHOOK SETUP =================
@app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    return 'Invalid content type', 403

@app.route('/')
def health_check():
    # Ping UptimeRobot on health check
    ping_uptime_robot()
    return jsonify({"status": "ok", "time": str(get_indian_time())})

def set_webhook():
    """Set up webhook for Telegram bot"""
    try:
        bot.remove_webhook()
        time.sleep(1)
        webhook_url = f"{SERVER_URL}/{BOT_TOKEN}"
        bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook set to: {webhook_url}")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")

# ================= MAIN =================
if __name__ == '__main__':
    logger.info("🤖 Starting bot...")
    
    # Initialize database
    initialize_database()
    
    # Set up webhook
    set_webhook()
    
    # Start Flask server
    app.run(host='0.0.0.0', port=WEBHOOK_PORT)
