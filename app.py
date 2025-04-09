#!/usr/bin/env python3
"""
Telegram Prediction Bot - Enhanced Version
A robust, efficient bot for generating predictions with referral and membership requirements.
"""

# ================= IMPORTS & INITIALIZATION =================
import os
import random
import time
import pytz
from datetime import datetime, timedelta
import logging
from threading import Thread, Lock
from flask import Flask, request, jsonify
import telebot
import psycopg2
from psycopg2 import pool
from urllib.parse import urlparse
from contextlib import contextmanager
from collections import OrderedDict
from tenacity import retry, stop_after_attempt, wait_exponential
from functools import wraps
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================= ENHANCED CACHE IMPLEMENTATION =================
class ExpiringCache:
    """
    Thread-safe cache with expiration and size limits.
    Uses OrderedDict for LRU eviction when max_size is reached.
    """
    def __init__(self, max_size=1000, ttl=300):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl  # seconds
        self.lock = Lock()

    def __setitem__(self, key, value):
        """Add item to cache with current timestamp"""
        with self.lock:
            self.cache[key] = (time.time(), value)
            self._cleanup()

    def __getitem__(self, key):
        """Get item from cache if not expired"""
        with self.lock:
            timestamp, value = self.cache[key]
            if time.time() - timestamp > self.ttl:
                del self.cache[key]
                raise KeyError("Expired")
            return value

    def get(self, key, default=None):
        """Safe get with default value"""
        try:
            return self[key]
        except KeyError:
            return default

    def pop(self, key, default=None):
        """Remove and return item if exists and not expired"""
        with self.lock:
            try:
                timestamp, value = self.cache.pop(key)
                if time.time() - timestamp > self.ttl:
                    return default
                return value
            except KeyError:
                return default

    def _cleanup(self):
        """Remove expired items and enforce max size"""
        now = time.time()
        # Remove expired items
        expired_keys = [k for k, (ts, _) in self.cache.items() if now - ts > self.ttl]
        for key in expired_keys:
            del self.cache[key]
        # Enforce max size using LRU policy
        while len(self.cache) > self.max_size:
            self.cache.popitem(last=False)

# ================= LOGGING SETUP =================
def setup_logging():
    """Configure structured JSON logging for better analysis"""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # JSON formatter for structured logs
    json_formatter = logging.Formatter(
        '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s", '
        '"module": "%(module)s", "function": "%(funcName)s", "line": %(lineno)d}'
    )
    
    # File handler
    file_handler = logging.FileHandler('bot.log')
    file_handler.setFormatter(json_formatter)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(json_formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logging()

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
UPTIME_ROBOT_URL = os.getenv('UPTIME_ROBOT_URL')

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

# Initialize bot and Flask app
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ================= GLOBAL TRACKERS =================
first_time_users = set()  # Tracks users who received their first prediction
cooldowns = {}  # Tracks user cooldowns
cooldown_lock = Lock()  # Thread-safe cooldown access

# Extended cache TTLs for better performance
membership_cache = ExpiringCache(max_size=5000, ttl=1800)  # 30 minute TTL
referral_cache = ExpiringCache(max_size=5000, ttl=3600)     # 1 hour TTL

# ================= DATABASE CONNECTION POOL =================
db_pool = None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def init_db_pool():
    """Initialize the database connection pool with retry logic"""
    global db_pool
    try:
        db_url = os.getenv('DATABASE_URL')
        if db_url:
            result = urlparse(db_url)
            db_pool = psycopg2.pool.SimpleConnectionPool(
                1, 20,
                database=result.path[1:],
                user=result.username,
                password=result.password,
                host=result.hostname,
                port=result.port
            )
        else:
            db_pool = psycopg2.pool.SimpleConnectionPool(
                1, 20,
                dbname=os.getenv('DB_NAME', 'telegram_bot'),
                user=os.getenv('DB_USER', 'postgres'),
                password=os.getenv('DB_PASSWORD', ''),
                host=os.getenv('DB_HOST', 'localhost')
            )
        logger.info("Database connection pool initialized successfully")
    except Exception as e:
        logger.error(f"Database pool initialization error (attempt {init_db_pool.retry.statistics['attempt_number']}): {e}")
        raise

@contextmanager
def db_connection():
    """
    Context manager for database connections.
    Yields a connection from the pool and ensures it's returned.
    """
    conn = None
    try:
        conn = db_pool.getconn()
        yield conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise
    finally:
        if conn:
            db_pool.putconn(conn)

@contextmanager
def db_cursor():
    """
    Context manager for database cursors.
    Handles transactions (commit/rollback) and cursor cleanup.
    """
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

def check_db_connection():
    """Verify database connectivity"""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1")
            return True
    except Exception as e:
        logger.error(f"Database connection check failed: {e}")
        return False

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
        logger.info("Database tables initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        raise

# ================= TELEGRAM API UTILITIES =================
def create_retry_session():
    """Create a requests session with retry logic"""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504]
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session

retry_session = create_retry_session()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def safe_telegram_call(func, *args, **kwargs):
    """
    Wrapper for Telegram API calls with retry logic.
    Logs attempts and errors for better debugging.
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        attempt = safe_telegram_call.retry.statistics['attempt_number']
        logger.warning(f"Telegram API call failed (attempt {attempt}): {e}")
        raise

def get_user_status(user_id):
    """
    Get comprehensive user status in a single call.
    Returns dict with is_member, referral_count, and is_admin status.
    """
    status = {
        'is_member': membership_cache.get(user_id),
        'referral_count': referral_cache.get(user_id),
        'is_admin': False
    }
    
    # Check admin status first (from local DB)
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1 FROM admins WHERE user_id = %s", (user_id,))
            status['is_admin'] = cur.fetchone() is not None
    except Exception as e:
        logger.error(f"Admin check error for user {user_id}: {e}")
    
    # Check membership if not cached
    if status['is_member'] is None:
        try:
            member = safe_telegram_call(bot.get_chat_member, f"@{CHANNEL_USERNAME}", user_id)
            status['is_member'] = member.status in ["member", "administrator", "creator"]
            membership_cache[user_id] = status['is_member']
        except Exception as e:
            logger.error(f"Membership check error for user {user_id}: {e}")
            status['is_member'] = False
    
    # Check referral count if not cached
    if status['referral_count'] is None:
        try:
            with db_cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM referrals WHERE referrer_id = %s",
                    (user_id,)
                )
                status['referral_count'] = cur.fetchone()[0]
                referral_cache[user_id] = status['referral_count']
        except Exception as e:
            logger.error(f"Referral count error for user {user_id}: {e}")
            status['referral_count'] = 0
    
    return status

# ================= UTILITY FUNCTIONS =================
def send_batch_messages(bot, user_ids, send_func, *args, **kwargs):
    """
    Send messages in batches to avoid rate limits.
    Returns tuple of (success_count, failure_count).
    """
    BATCH_SIZE = 30  # Telegram's limit is about 30 messages per second
    DELAY = 1  # 1 second delay between batches
    
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
        time.sleep(DELAY)
    
    return success, failures

def get_indian_time():
    """Get current time in Indian timezone"""
    return datetime.now(INDIAN_TIMEZONE)

def format_time(dt):
    """Format time as HH:MM"""
    return dt.strftime("%H:%M")

def generate_prediction():
    """Generate a random prediction with safe value"""
    pred = round(random.uniform(2.50, 4.50), 2)
    safe = round(random.uniform(1.50, min(pred, 3.0)), 2)
    future_time = get_indian_time() + timedelta(seconds=PREDICTION_DELAY)
    return format_time(future_time), pred, safe

def get_share_markup(user_id):
    """Create inline keyboard for sharing the bot"""
    markup = telebot.types.InlineKeyboardMarkup()
    share_btn = telebot.types.InlineKeyboardButton(
        f"{ROCKET} Share Bot {ROCKET}",
        url=f"https://t.me/share/url?url=t.me/{BOT_USERNAME}?start={user_id}&text=Check%20out%20this%20awesome%20prediction%20bot!"
    )
    markup.add(share_btn)
    markup.add(telebot.types.InlineKeyboardButton("✅ Verify Shares", callback_data="verify_shares"))
    return markup

def get_main_markup(user_id):
    """Create main menu inline keyboard"""
    markup = telebot.types.InlineKeyboardMarkup()
    user_status = get_user_status(user_id)
    
    if user_status['is_member'] and (SHARES_REQUIRED == 0 or user_status['referral_count'] >= SHARES_REQUIRED):
        markup.row(
            telebot.types.InlineKeyboardButton(f"{ROCKET} Generate Prediction", callback_data="get_prediction"),
            telebot.types.InlineKeyboardButton(f"📡 Request Live Prediction", callback_data="request_live")
        )
    return markup

def get_admin_markup():
    """Create admin panel inline keyboard"""
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
    """Notify all admins with error handling"""
    try:
        admins = []
        with db_cursor() as cur:
            cur.execute("SELECT user_id FROM admins")
            admins = [str(row[0]) for row in cur.fetchall()]
        
        for admin_id in admins:
            try:
                bot.send_message(admin_id, message)
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")
    except Exception as e:
        logger.error(f"Error getting admin list: {e}")

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
            retry_session.get(UPTIME_ROBOT_URL, timeout=5)
            logger.info("Successfully pinged UptimeRobot")
        except Exception as e:
            logger.error(f"Error pinging UptimeRobot: {e}")

# ================= BOT MESSAGE HANDLERS =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    """Handle /start and /help commands"""
    try:
        user_id = message.chat.id
        user_info = message.from_user
        
        # Clear cache for fresh status check
        membership_cache.pop(user_id, None)
        referral_cache.pop(user_id, None)
        
        # Process referral if present in command and user is channel member
        if len(message.text.split()) > 1:
            user_status = get_user_status(user_id)
            if user_status['is_member']:
                try:
                    referrer_str = message.text.split()[1]
                    referrer_id = safe_int_convert(referrer_str)
                    
                    # Validate referral
                    if referrer_id != 0 and referrer_id != user_id:
                        with db_cursor() as cur:
                            # Check if user is already registered
                            cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
                            if not cur.fetchone():  # Only proceed if new user
                                if save_referral(referrer_id, user_id):
                                    logger.info(f"New referral: {referrer_id} -> {user_id}")
                                    # Clear relevant caches
                                    referral_cache.pop(referrer_id, None)
                                    membership_cache.pop(user_id, None)
                except Exception as e:
                    logger.error(f"Referral processing error: {e}")
        
        # Get fresh user status after potential referral processing
        user_status = get_user_status(user_id)
        
        # Welcome message template
        welcome_msg = (
            f"{GRAPH} *WELCOME TO AI-POWERED PREDICTION BOT* {GRAPH}\n\n"
            f"{DIAMOND} Use suggested assurance for risk management\n"
            f"{DIAMOND} Follow cooldown periods\n\n"
            f"{SHIELD} *VIP Channel:* @{CHANNEL_USERNAME}"
        )
        
        # Check user access level
        if user_status['is_member'] and (SHARES_REQUIRED == 0 or user_status['referral_count'] >= SHARES_REQUIRED):
            # Eligible user - show main menu
            bot.send_message(user_id, welcome_msg, reply_markup=get_main_markup(user_id), parse_mode="Markdown")
            # Save user if not already in database
            save_user_if_eligible(user_info)
        elif not user_status['is_member']:
            # Not a channel member - prompt to join
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(
                telebot.types.InlineKeyboardButton("Join VIP Channel", url=f"https://t.me/{CHANNEL_USERNAME}"),
                telebot.types.InlineKeyboardButton("Verify Membership", callback_data="check_membership")
            )
            bot.send_message(
                user_id, 
                f"{CROSS} *PREMIUM ACCESS REQUIRED*\n\nJoin @{CHANNEL_USERNAME} then verify.", 
                reply_markup=markup, 
                parse_mode="Markdown"
            )
        else:
            # Member but needs more referrals
            shares_count = user_status['referral_count']
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
        logger.error(f"Welcome message error for user {message.chat.id}: {e}")
        bot.send_message(message.chat.id, "⚠️ An error occurred. Please try again.")

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    """Handle /admin command"""
    try:
        user_id = message.chat.id
        user_status = get_user_status(user_id)
        
        if user_status['is_admin']:
            bot.send_message(user_id, "🛠 *Admin Panel* 🛠", reply_markup=get_admin_markup(), parse_mode="Markdown")
        else:
            bot.send_message(user_id, "⛔ Unauthorized access!")
    except Exception as e:
        logger.error(f"Admin panel error for user {user_id}: {e}")

# ================= CALLBACK QUERY HANDLERS =================
@bot.callback_query_handler(func=lambda call: call.data == "check_membership")
def check_membership(call):
    """Handle membership verification callback"""
    try:
        user_id = call.message.chat.id
        # Clear cache for fresh check
        membership_cache.pop(user_id, None)
        referral_cache.pop(user_id, None)
        
        user_status = get_user_status(user_id)
        
        if user_status['is_member']:
            if SHARES_REQUIRED == 0 or user_status['referral_count'] >= SHARES_REQUIRED:
                bot.answer_callback_query(call.id, "✅ Fully verified! You can now get predictions.")
                send_welcome(call.message)
            else:
                shares_needed = SHARES_REQUIRED - user_status['referral_count']
                bot.answer_callback_query(
                    call.id, 
                    f"✅ Membership verified! Need {shares_needed} more referrals.", 
                    show_alert=True
                )
                send_welcome(call.message)
        else:
            bot.answer_callback_query(call.id, "❌ Join channel first!", show_alert=True)
    except Exception as e:
        logger.error(f"Membership check error for user {call.message.chat.id}: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "verify_shares")
def verify_shares(call):
    """Handle share verification callback"""
    try:
        user_id = call.message.chat.id
        # Clear cache for fresh check
        membership_cache.pop(user_id, None)
        referral_cache.pop(user_id, None)
        
        user_status = get_user_status(user_id)
        
        if not user_status['is_member']:
            bot.answer_callback_query(call.id, "❌ Join channel first, then verify!", show_alert=True)
            return
            
        # Save user if they now meet requirements
        if SHARES_REQUIRED == 0 or user_status['referral_count'] >= SHARES_REQUIRED:
            save_user_if_eligible(call.from_user)
            
        if SHARES_REQUIRED == 0 or user_status['referral_count'] >= SHARES_REQUIRED:
            bot.answer_callback_query(call.id, "✅ Fully verified! You can now get predictions.")
            send_welcome(call.message)
        else:
            needed = SHARES_REQUIRED - user_status['referral_count']
            bot.answer_callback_query(
                call.id, 
                f"❌ Need {needed} more valid referrals (users who joined channel)", 
                show_alert=True
            )
    except Exception as e:
        logger.error(f"Share verification error for user {call.message.chat.id}: {e}")
        bot.answer_callback_query(call.id, "⚠️ Error verifying shares. Please try again.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "get_prediction")
def handle_prediction(call):
    """Handle prediction generation callback"""
    try:
        user_id = call.message.chat.id
        user_status = get_user_status(user_id)
        
        # Verify access
        if not user_status['is_member']:
            bot.answer_callback_query(call.id, "❌ Join channel first!", show_alert=True)
            return
            
        if SHARES_REQUIRED > 0 and user_status['referral_count'] < SHARES_REQUIRED:
            bot.answer_callback_query(call.id, "❌ Complete sharing first!", show_alert=True)
            return
            
        # Check cooldown
        with cooldown_lock:
            if user_id in cooldowns and (remaining := cooldowns[user_id] - time.time()) > 0:
                mins, secs = divmod(int(remaining), 60)
                bot.answer_callback_query(call.id, f"{LOCK} Wait {mins}m {secs}s", show_alert=True)
                return

        # Remove inline keyboard
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception as e:
            logger.warning(f"Couldn't remove reply markup: {e}")

        # Send welcome sticker for first-time users
        if user_id not in first_time_users:
            try:
                safe_telegram_call(bot.send_sticker, user_id, ROCKET_STICKER_ID)
                first_time_users.add(user_id)
            except Exception as e:
                logger.warning(f"Couldn't send sticker to {user_id}: {e}")

        # Generate and send prediction
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
        
        safe_telegram_call(
            bot.send_message, 
            user_id, 
            prediction_msg, 
            reply_markup=get_main_markup(user_id), 
            parse_mode="Markdown"
        )
        
        # Update cooldown
        with cooldown_lock:
            cooldowns[user_id] = time.time() + COOLDOWN_SECONDS
            
        bot.answer_callback_query(call.id, "✅ Prediction generated!")
        
    except Exception as e:
        logger.error(f"Prediction generation error for user {call.message.chat.id}: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "request_live")
def request_live_prediction(call):
    """Handle live prediction request callback"""
    try:
        user_id = call.message.chat.id
        user_status = get_user_status(user_id)
        
        # Verify access
        if not user_status['is_member']:
            bot.answer_callback_query(call.id, "❌ Join channel first!", show_alert=True)
            return
            
        if SHARES_REQUIRED > 0 and user_status['referral_count'] < SHARES_REQUIRED:
            bot.answer_callback_query(call.id, "❌ Complete sharing first!", show_alert=True)
            return
            
        # Save request
        if save_live_request(user_id):
            total_requests = count_live_requests()
            bot.answer_callback_query(
                call.id, 
                f"✅ Your request sent, admin will be notified\n{total_requests} members have requested", 
                show_alert=True
            )
            notify_admins(f"👋 Live prediction request from user {user_id}")
        else:
            bot.answer_callback_query(call.id, "❌ You already have a pending request!", show_alert=True)
            
    except Exception as e:
        logger.error(f"Live prediction request error for user {call.message.chat.id}: {e}")

# ================= ADMIN CALLBACK HANDLERS =================
@bot.callback_query_handler(func=lambda call: call.data == "send_prediction")
def send_prediction_menu(call):
    """Admin menu for sending predictions"""
    try:
        user_id = call.message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
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
        logger.error(f"Send prediction menu error for admin {user_id}: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "back_to_admin")
def back_to_admin(call):
    """Return to admin main menu"""
    try:
        user_id = call.message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
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
        logger.error(f"Back to admin error for admin {user_id}: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "send_text")
def ask_for_text_message(call):
    """Prompt admin for text message to broadcast"""
    try:
        user_id = call.message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        msg = bot.send_message(user_id, "✍️ Enter the text message to send to verified users:")
        bot.register_next_step_handler(msg, process_text_message)
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ask for text message error for admin {user_id}: {e}")

def process_text_message(message):
    """Process and broadcast text message to eligible users"""
    try:
        user_id = message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
            return
            
        text_content = message.text
        verified_users = get_users()
        
        # Filter eligible users
        eligible_users = []
        for user in verified_users:
            uid = int(user['user_id'])
            user_status = get_user_status(uid)
            if user_status['is_member'] and (SHARES_REQUIRED == 0 or user_status['referral_count'] >= SHARES_REQUIRED):
                eligible_users.append(uid)
        
        # Send in batches
        success, failures = send_batch_messages(
            bot,
            eligible_users,
            lambda uid: bot.send_message(uid, f"🟢 *LIVE PREDICTION*\n\n{text_content}", parse_mode="Markdown")
        )
                
        bot.send_message(user_id, f"✅ Text sent to {success} users\n❌ Failed for {failures} users")
        
    except Exception as e:
        logger.error(f"Text message processing error for admin {message.chat.id}: {e}")
        bot.send_message(message.chat.id, f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "send_image")
def ask_for_image(call):
    """Prompt admin for image to broadcast"""
    try:
        user_id = call.message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        msg = bot.send_message(user_id, "🖼️ Send the image you want to broadcast (send as photo):")
        bot.register_next_step_handler(msg, process_image_message)
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ask for image error for admin {user_id}: {e}")

def process_image_message(message):
    """Process and broadcast image to eligible users"""
    try:
        user_id = message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
            return
            
        if not message.photo:
            bot.send_message(user_id, "❌ Please send an image as a photo.")
            return
            
        photo = message.photo[-1].file_id
        caption = message.caption if message.caption else "📡 *LIVE PREDICTION*"
        
        verified_users = get_users()
        
        # Filter eligible users
        eligible_users = []
        for user in verified_users:
            uid = int(user['user_id'])
            user_status = get_user_status(uid)
            if user_status['is_member'] and (SHARES_REQUIRED == 0 or user_status['referral_count'] >= SHARES_REQUIRED):
                eligible_users.append(uid)
        
        # Send in batches
        success, failures = send_batch_messages(
            bot,
            eligible_users,
            lambda uid: bot.send_photo(uid, photo, caption=caption, parse_mode="Markdown")
        )
                
        bot.send_message(user_id, f"✅ Image sent to {success} users\n❌ Failed for {failures} users")
        
    except Exception as e:
        logger.error(f"Image processing error for admin {message.chat.id}: {e}")
        bot.send_message(message.chat.id, f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "send_voice")
def ask_for_voice(call):
    """Prompt admin for voice message to broadcast"""
    try:
        user_id = call.message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        msg = bot.send_message(user_id, "🎤 Send the voice message you want to broadcast:")
        bot.register_next_step_handler(msg, process_voice_message)
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ask for voice error for admin {user_id}: {e}")

def process_voice_message(message):
    """Process and broadcast voice message to eligible users"""
    try:
        user_id = message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
            return
            
        if not message.voice:
            bot.send_message(user_id, "❌ Please send a voice message.")
            return
            
        voice = message.voice.file_id
        caption = message.caption if message.caption else "📡 *LIVE PREDICTION*"
        
        verified_users = get_users()
        
        # Filter eligible users
        eligible_users = []
        for user in verified_users:
            uid = int(user['user_id'])
            user_status = get_user_status(uid)
            if user_status['is_member'] and (SHARES_REQUIRED == 0 or user_status['referral_count'] >= SHARES_REQUIRED):
                eligible_users.append(uid)
        
        # Send in batches
        success, failures = send_batch_messages(
            bot,
            eligible_users,
            lambda uid: bot.send_voice(uid, voice, caption=caption, parse_mode="Markdown")
        )
                
        bot.send_message(user_id, f"✅ Voice message sent to {success} users\n❌ Failed for {failures} users")
        
    except Exception as e:
        logger.error(f"Voice processing error for admin {message.chat.id}: {e}")
        bot.send_message(message.chat.id, f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "send_sticker")
def ask_for_sticker(call):
    """Prompt admin for sticker to broadcast"""
    try:
        user_id = call.message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
            bot.answer_callback_query(call.id, "⛔ Unauthorized access!")
            return
            
        msg = bot.send_message(user_id, "😄 Send the sticker you want to broadcast:")
        bot.register_next_step_handler(msg, process_sticker_message)
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ask for sticker error for admin {user_id}: {e}")

def process_sticker_message(message):
    """Process and broadcast sticker to eligible users"""
    try:
        user_id = message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
            return
            
        if not message.sticker:
            bot.send_message(user_id, "❌ Please send a sticker.")
            return
            
        sticker = message.sticker.file_id
        
        verified_users = get_users()
        
        # Filter eligible users
        eligible_users = []
        for user in verified_users:
            uid = int(user['user_id'])
            user_status = get_user_status(uid)
            if user_status['is_member'] and (SHARES_REQUIRED == 0 or user_status['referral_count'] >= SHARES_REQUIRED):
                eligible_users.append(uid)
        
        # Send in batches
        success, failures = send_batch_messages(
            bot,
            eligible_users,
            lambda uid: bot.send_sticker(uid, sticker)
        )
                
        bot.send_message(user_id, f"✅ Sticker sent to {success} users\n❌ Failed for {failures} users")
        
    except Exception as e:
        logger.error(f"Sticker processing error for admin {message.chat.id}: {e}")
        bot.send_message(message.chat.id, f"❌ Error: {e}")
        
@bot.callback_query_handler(func=lambda call: call.data in ["check_requests", "clear_requests", "check_users"])
def admin_actions(call):
    """Handle various admin actions"""
    try:
        user_id = call.message.chat.id
        user_status = get_user_status(user_id)
        
        if not user_status['is_admin']:
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
        logger.error(f"Admin action error for admin {user_id}: {e}")

# ================= DATABASE OPERATIONS =================
def save_user_if_eligible(user_info):
    """Save user to database only if they meet all requirements"""
    user_id = user_info.id
    
    # Clear cache for fresh status
    membership_cache.pop(user_id, None)
    referral_cache.pop(user_id, None)
    
    user_status = get_user_status(user_id)
    
    try:
        with db_cursor() as cur:
            # Check if user exists first
            cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
            if cur.fetchone():
                return True  # User already exists
                
            # Only save if they meet requirements
            if user_status['is_member'] and (SHARES_REQUIRED == 0 or user_status['referral_count'] >= SHARES_REQUIRED):
                cur.execute(
                    """
                    INSERT INTO users (user_id, username, first_name, last_name)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (user_info.id, user_info.username, user_info.first_name, user_info.last_name)
                )
                # Clear referral cache for referrers
                cur.execute("SELECT referrer_id FROM referrals WHERE referred_id = %s", (user_id,))
                for row in cur.fetchall():
                    referral_cache.pop(row[0], None)
                return True
            return False
    except Exception as e:
        logger.error(f"Error saving eligible user {user_id}: {e}")
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
            # Clear referral cache for referrer
            referral_cache.pop(referrer_id, None)
            return True
    except Exception as e:
        logger.error(f"Error saving referral {referrer_id} -> {referred_id}: {e}")
        return False

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
            return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"Error saving live request for user {user_id}: {e}")
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

# ================= WEBHOOK & HEALTH ENDPOINTS =================
WEBHOOK_PATH = f'/{BOT_TOKEN}/{os.getenv("WEBHOOK_SECRET")}'

@app.route(WEBHOOK_PATH, methods=['POST'])
def secure_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    return 'Invalid content type', 403


@app.route('/')
def index():
    """Basic health check endpoint"""
    ping_uptime_robot()
    return jsonify({"status": "ok", "time": str(get_indian_time())})

@app.route('/health')
def health_check():
    """Comprehensive health check endpoint"""
    try:
        # Test database connection
        db_status = "connected" if check_db_connection() else "disconnected"
        
        return jsonify({
            "status": "healthy",
            "database": db_status,
            "cache": {
                "membership": len(membership_cache.cache),
                "referral": len(referral_cache.cache)
            },
            "cooldowns": len(cooldowns),
            "timestamp": str(datetime.now(INDIAN_TIMEZONE))
        })
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": str(datetime.now(INDIAN_TIMEZONE))
        }), 500

def set_secure_webhook():
    try:
        # Remove any existing webhook
        bot.remove_webhook()
        time.sleep(2)
        
        # Set new secure webhook
        webhook_url = f"{SERVER_URL}{WEBHOOK_PATH}"
        bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True
        )
        logger.info(f"Webhook securely set to: {webhook_url}")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        notify_admins(f"🚨 Webhook setup failed: {e}")


def verify_webhook_ownership():
    current = bot.get_webhook_info()
    expected = f"{SERVER_URL}{WEBHOOK_PATH}"
    if current.url != expected:
        logger.critical(f"WEBHOOK HIJACKED! Resetting...")
        set_secure_webhook()
        notify_admins("🚨 Webhook hijack detected and reset!")

# ================= MAIN EXECUTION =================
if __name__ == '__main__':
    logger.info("Starting bot...")
    init_db_pool()
    initialize_database()
    
    # Secure webhook setup
    set_secure_webhook()  # NEW FUNCTION
    
    # Start periodic webhook checks (every 1 hour)
    Thread(target=lambda: [time.sleep(3600), verify_webhook_ownership()], daemon=True).start()
    
    app.run(host='0.0.0.0', port=WEBHOOK_PORT)
