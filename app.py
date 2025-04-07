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
from psycopg2 import sql, pool
from urllib.parse import urlparse
from functools import lru_cache

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

# ================= DATABASE CONNECTION POOL =================
class DatabaseConnection:
    _pool = None

    @classmethod
    def initialize_pool(cls):
        try:
            db_url = os.getenv('DATABASE_URL')
            if db_url:
                result = urlparse(db_url)
                cls._pool = psycopg2.pool.SimpleConnectionPool(
                    minconn=1,
                    maxconn=10,
                    database=result.path[1:],
                    user=result.username,
                    password=result.password,
                    host=result.hostname,
                    port=result.port
                )
            else:
                # For local testing
                cls._pool = psycopg2.pool.SimpleConnectionPool(
                    minconn=1,
                    maxconn=10,
                    dbname=os.getenv('DB_NAME', 'telegram_bot'),
                    user=os.getenv('DB_USER', 'postgres'),
                    password=os.getenv('DB_PASSWORD', ''),
                    host=os.getenv('DB_HOST', 'localhost')
                )
            logger.info("Database connection pool initialized")
        except Exception as e:
            logger.error(f"Database connection pool error: {e}")
            raise

    @classmethod
    def get_connection(cls):
        if cls._pool is None:
            cls.initialize_pool()
        return cls._pool.getconn()

    @classmethod
    def return_connection(cls, conn):
        if cls._pool is not None and conn:
            cls._pool.putconn(conn)

    @classmethod
    def close_all_connections(cls):
        if cls._pool is not None:
            cls._pool.closeall()
            logger.info("All database connections closed")

# Initialize connection pool
DatabaseConnection.initialize_pool()

# Database context manager for cleaner connection handling
class DatabaseContext:
    def __enter__(self):
        self.conn = DatabaseConnection.get_connection()
        return self.conn.cursor()
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.conn.rollback()
            logger.error(f"Database error: {exc_val}")
        else:
            self.conn.commit()
        DatabaseConnection.return_connection(self.conn)

# ================= DATABASE FUNCTIONS =================
def initialize_database():
    """Create necessary tables if they don't exist"""
    commands = (
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username VARCHAR(255),
            first_name VARCHAR(255),
            last_name VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_member BOOLEAN DEFAULT FALSE,
            shares_count INTEGER DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id SERIAL PRIMARY KEY,
            referrer_id BIGINT NOT NULL,
            referred_id BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(referrer_id, referred_id),
            FOREIGN KEY (referrer_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS live_requests (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL UNIQUE
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_users_member ON users(is_member)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_users_shares ON users(shares_count)
        """
    )
    
    with DatabaseContext() as cur:
        for command in commands:
            cur.execute(command)

@lru_cache(maxsize=128)
def get_admins():
    """Get cached list of admin user IDs"""
    with DatabaseContext() as cur:
        cur.execute("SELECT user_id FROM admins")
        return [str(row[0]) for row in cur.fetchall()]

def is_admin(user_id):
    """Check if user is admin"""
    return str(user_id) in get_admins()

def update_user_membership(user_id, is_member_status):
    """Update user's membership status"""
    with DatabaseContext() as cur:
        cur.execute(
            "UPDATE users SET is_member = %s WHERE user_id = %s",
            (is_member_status, user_id)
        )

def update_user_shares(user_id, shares_count):
    """Update user's shares count"""
    with DatabaseContext() as cur:
        cur.execute(
            "UPDATE users SET shares_count = %s WHERE user_id = %s",
            (shares_count, user_id)
        )

def save_user_if_eligible(user_info):
    """Save user to database only if they meet all requirements"""
    user_id = user_info.id
    
    # First check membership status
    is_member_status = is_member(user_id)
    shares_count = count_user_referrals(user_id)
    is_eligible = is_member_status and (SHARES_REQUIRED == 0 or shares_count >= SHARES_REQUIRED)
    
    with DatabaseContext() as cur:
        # Check if user exists
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        if cur.fetchone():
            # Update existing user's status
            cur.execute(
                """
                UPDATE users 
                SET username = %s, first_name = %s, last_name = %s, 
                    is_member = %s, shares_count = %s
                WHERE user_id = %s
                """,
                (
                    user_info.username, user_info.first_name, user_info.last_name,
                    is_member_status, shares_count, user_id
                )
            )
            return is_eligible
        
        # Only insert if eligible
        if is_eligible:
            cur.execute(
                """
                INSERT INTO users 
                (user_id, username, first_name, last_name, is_member, shares_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id, user_info.username, user_info.first_name, 
                    user_info.last_name, is_member_status, shares_count
                )
            )
            return True
    return False

def save_referral(referrer_id, referred_id):
    """Save referral relationship if referrer exists"""
    with DatabaseContext() as cur:
        # Check if referrer exists
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (referrer_id,))
        if not cur.fetchone():
            return False
            
        try:
            cur.execute(
                """
                INSERT INTO referrals (referrer_id, referred_id)
                VALUES (%s, %s)
                ON CONFLICT (referrer_id, referred_id) DO NOTHING
                RETURNING 1
                """,
                (referrer_id, referred_id)
            )
            if cur.fetchone():
                # Update referrer's shares count
                shares_count = count_user_referrals(referrer_id)
                update_user_shares(referrer_id, shares_count)
                return True
            return False
        except Exception as e:
            logger.error(f"Error saving referral: {e}")
            return False

def count_user_referrals(user_id):
    """Count how many referrals a user has"""
    with DatabaseContext() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = %s",
            (user_id,)
        )
        return cur.fetchone()[0] or 0

def save_live_request(user_id):
    """Save a live prediction request if user exists"""
    with DatabaseContext() as cur:
        # Check if user exists and is eligible
        cur.execute(
            """
            SELECT 1 FROM users 
            WHERE user_id = %s AND is_member = TRUE 
            AND (shares_count >= %s OR %s = 0)
            """,
            (user_id, SHARES_REQUIRED, SHARES_REQUIRED)
        )
        if not cur.fetchone():
            return False
            
        try:
            cur.execute(
                """
                INSERT INTO live_requests (user_id) 
                VALUES (%s)
                ON CONFLICT (user_id) DO NOTHING
                RETURNING id
                """,
                (user_id,)
            )
            return cur.fetchone() is not None
        except Exception as e:
            logger.error(f"Error saving live request: {e}")
            return False

def count_live_requests():
    """Count total live requests"""
    with DatabaseContext() as cur:
        cur.execute("SELECT COUNT(*) FROM live_requests")
        return cur.fetchone()[0] or 0

def clear_live_requests():
    """Clear all live requests"""
    with DatabaseContext() as cur:
        cur.execute("TRUNCATE live_requests")
        return True

def get_live_requests():
    """Get all live prediction requests"""
    with DatabaseContext() as cur:
        cur.execute("SELECT user_id FROM live_requests")
        return [str(row[0]) for row in cur.fetchall()]

def get_users():
    """Get all users from database"""
    with DatabaseContext() as cur:
        cur.execute("SELECT user_id, username, first_name, last_name FROM users")
        return [
            {
                'user_id': str(row[0]),
                'username': row[1],
                'first_name': row[2],
                'last_name': row[3]
            }
            for row in cur.fetchall()
        ]

def get_eligible_users():
    """Get users who are members and have enough shares"""
    with DatabaseContext() as cur:
        cur.execute(
            """
            SELECT user_id, username, first_name, last_name 
            FROM users 
            WHERE is_member = TRUE AND (shares_count >= %s OR %s = 0)
            """,
            (SHARES_REQUIRED, SHARES_REQUIRED)
        )
        return [
            {
                'user_id': str(row[0]),
                'username': row[1],
                'first_name': row[2],
                'last_name': row[3]
            }
            for row in cur.fetchall()
        ]

# ================= UTILITY FUNCTIONS =================
def get_indian_time():
    """Get current time in Indian timezone"""
    return datetime.now(INDIAN_TIMEZONE)

def format_time(dt):
    """Format time as HH:MM"""
    return dt.strftime("%H:%M")

def is_member(user_id):
    """Check if user is member of channel with caching"""
    try:
        member = bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        status = member.status in ["member", "administrator", "creator"]
        # Update database with current membership status
        update_user_membership(user_id, status)
        return status
    except Exception as e:
        logger.error(f"Membership check error: {e}")
        return False

def has_shared_enough(user_id):
    """Check if user has enough referrals"""
    with DatabaseContext() as cur:
        cur.execute(
            "SELECT shares_count >= %s OR %s = 0 FROM users WHERE user_id = %s",
            (SHARES_REQUIRED, SHARES_REQUIRED, user_id)
        )
        result = cur.fetchone()
        return result[0] if result else False

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
    return markup

def notify_admins(message):
    """Notify all admins"""
    admin_ids = get_admins()
    for admin_id in admin_ids:
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

def batch_send_messages(user_ids, send_func, *args, **kwargs):
    """Batch send messages to users with error handling"""
    success = 0
    failures = 0
    batch_size = 30  # Telegram's rate limit is about 30 messages per second
    
    for i in range(0, len(user_ids), batch_size):
        batch = user_ids[i:i + batch_size]
        for user_id in batch:
            try:
                send_func(user_id, *args, **kwargs)
                success += 1
            except Exception as e:
                logger.error(f"Failed to send to {user_id}: {e}")
                failures += 1
        time.sleep(1)  # Rate limiting
    
    return success, failures

# ================= BOT HANDLERS =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    try:
        user_id = message.chat.id
        user_info = message.from_user
        
        # Process referral only if user joined channel
        if len(message.text.split()) > 1 and is_member(user_id):
            try:
                referrer_str = message.text.split()[1]
                referrer_id = safe_int_convert(referrer_str)
                if referrer_id != 0 and referrer_id != user_id:
                    if save_referral(referrer_id, user_id):
                        logger.info(f"New verified referral: {referrer_id} -> {user_id} (channel member)")
            except Exception as e:
                logger.error(f"Referral processing error: {e}")

        # Save/update user with current status
        is_eligible = save_user_if_eligible(user_info)
        
        welcome_msg = (
            f"{GRAPH} *WELCOME TO AI-POWERED PREDICTION BOT* {GRAPH}\n\n"
            f"{DIAMOND} Use suggested assurance for risk management\n"
            f"{DIAMOND} Follow cooldown periods\n\n"
            f"{SHIELD} *VIP Channel:* @{CHANNEL_USERNAME}"
        )
        
        if is_member(user_id):
            if has_shared_enough(user_id):
                bot.send_message(user_id, welcome_msg, reply_markup=get_main_markup(user_id), parse_mode="Markdown")
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
        else:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(
                telebot.types.InlineKeyboardButton("Join VIP Channel", url=f"https://t.me/{CHANNEL_USERNAME}"),
                telebot.types.InlineKeyboardButton("Verify Membership", callback_data="check_membership")
            )
            bot.send_message(user_id, f"{CROSS} *PREMIUM ACCESS REQUIRED*\n\nJoin @{CHANNEL_USERNAME} then verify.", 
                           reply_markup=markup, parse_mode="Markdown")
            
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
        if not is_member(user_id):
            bot.answer_callback_query(call.id, "❌ Join channel first, then verify!", show_alert=True)
            return
            
        # Update user status
        shares_count = count_user_referrals(user_id)
        save_user_if_eligible(call.from_user)
        
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
        verified_users = [user['user_id'] for user in get_eligible_users()]
        
        success, failures = batch_send_messages(
            verified_users,
            lambda uid: bot.send_message(uid, f"📡 *LIVE PREDICTION*\n\n{text_content}", parse_mode="Markdown")
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
        verified_users = [user['user_id'] for user in get_eligible_users()]
        
        success, failures = batch_send_messages(
            verified_users,
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
        verified_users = [user['user_id'] for user in get_eligible_users()]
        
        success, failures = batch_send_messages(
            verified_users,
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
        verified_users = [user['user_id'] for user in get_eligible_users()]
        
        success, failures = batch_send_messages(
            verified_users,
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
    
    # Clean up on exit
    DatabaseConnection.close_all_connections()
