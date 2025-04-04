import os
import telebot
import random
import time
import pytz
from datetime import datetime, timedelta
import logging
from threading import Thread

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME', 'testsub01')
BOT_USERNAME = os.getenv('BOT_USERNAME')
COOLDOWN_SECONDS = 120
PREDICTION_DELAY = 130
SHARES_REQUIRED = 1
INDIAN_TIMEZONE = pytz.timezone('Asia/Kolkata')

# Configure logging
os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Emojis and Stickers
ROCKET = "🚀"
LOCK = "🔒"
CHECK = "✅"
CROSS = "❌"
HOURGLASS = "⏳"
DIAMOND = "◆"
GRAPH = "📈"
SHIELD = "🛡️"
ROCKET_STICKER_ID = "CAACAgUAAxkBAAEL3xRmEeX3xQABHYYYr4YH1LQhUe3VdW8AAp4LAAIWjvlVjXjWbJQN0k80BA"

# Track users
first_time_users = set()
cooldowns = {}
referral_data = {}

# Initialize bot
bot = telebot.TeleBot(BOT_TOKEN)

# ================ UTILITY FUNCTIONS ================
def get_indian_time():
    return datetime.now(INDIAN_TIMEZONE)

def format_time(dt):
    return dt.strftime("%H:%M")

def is_member(user_id):
    try:
        member = bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Membership check error: {e}")
        return False

def count_valid_shares(user_id):
    return len(set(referral_data.get(user_id, [])))

def has_shared_enough(user_id):
    return count_valid_shares(user_id) >= SHARES_REQUIRED

def generate_prediction():
    pred = round(random.uniform(2.50, 4.50), 2)
    safe = round(random.uniform(1.50, min(pred, 3.0)), 2)
    future_time = get_indian_time() + timedelta(seconds=PREDICTION_DELAY)
    return format_time(future_time), pred, safe

def get_share_markup(user_id):
    markup = telebot.types.InlineKeyboardMarkup()
    share_btn = telebot.types.InlineKeyboardButton(
        f"{ROCKET} Share Bot {ROCKET}",
        url=f"https://t.me/share/url?url=t.me/{BOT_USERNAME}?start={user_id}&text=Check%20out%20this%20awesome%20prediction%20bot!"
    )
    markup.add(share_btn)
    markup.add(telebot.types.InlineKeyboardButton("✅ Verify Shares", callback_data="verify_shares"))
    return markup

# ============== BOT HANDLERS ==============
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    try:
        user_id = message.chat.id
        user_info = message.from_user
        
        # Referral handling
        if len(message.text.split()) > 1:
            try:
                referrer_id = int(message.text.split()[1])
                if referrer_id != user_id:
                    if referrer_id not in referral_data:
                        referral_data[referrer_id] = []
                    if user_id not in referral_data[referrer_id]:
                        referral_data[referrer_id].append(user_id)
                        logger.info(f"New referral: {referrer_id} -> {user_id}")
            except Exception as e:
                logger.error(f"Referral error: {e}")
        
        # Welcome message
        welcome_msg = (
            f"{GRAPH} *WELCOME TO AI-POWERED PREDICTION BOT* {GRAPH}\n\n"
            f"{DIAMOND} Use suggested assurance for risk management\n"
            f"{DIAMOND} Follow cooldown periods\n\n"
            f"{SHIELD} *VIP Channel:* @{CHANNEL_USERNAME}"
        )
        
        if not is_member(user_id):
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(
                telebot.types.InlineKeyboardButton("Join VIP Channel", url=f"https://t.me/{CHANNEL_USERNAME}"),
                telebot.types.InlineKeyboardButton("Verify Membership", callback_data="check_membership")
            )
            bot.send_message(user_id, f"{CROSS} *PREMIUM ACCESS REQUIRED*\n\nJoin @{CHANNEL_USERNAME} then verify.", reply_markup=markup, parse_mode="Markdown")
            return
        
        if not has_shared_enough(user_id):
            shares_count = count_valid_shares(user_id)
            share_msg = (
                f"{LOCK} *SHARE REQUIREMENT*\n\n"
                f"Refer {SHARES_REQUIRED} friends to unlock.\n"
                f"Current: {shares_count}/{SHARES_REQUIRED}\n\n"
                "1. Click 'Share Bot'\n"
                "2. Send to friends\n"
                "3. They must START the bot\n"
                "4. Verify after they join"
            )
            bot.send_message(user_id, share_msg, reply_markup=get_share_markup(user_id), parse_mode="Markdown")
            return
        
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(f"{ROCKET} Generate Prediction", callback_data="get_prediction"))
        bot.send_message(user_id, welcome_msg, reply_markup=markup, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Welcome error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "check_membership")
def check_membership(call):
    try:
        if is_member(call.message.chat.id):
            bot.answer_callback_query(call.id, "✅ Verified!")
            send_welcome(call.message)
        else:
            bot.answer_callback_query(call.id, "❌ Join channel first!", show_alert=True)
    except Exception as e:
        logger.error(f"Membership check error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "verify_shares")
def verify_shares(call):
    try:
        user_id = call.message.chat.id
        if has_shared_enough(user_id):
            bot.answer_callback_query(call.id, "✅ Sharing verified!")
            send_welcome(call.message)
        else:
            needed = SHARES_REQUIRED - count_valid_shares(user_id)
            bot.answer_callback_query(call.id, f"❌ Need {needed} more referrals", show_alert=True)
    except Exception as e:
        logger.error(f"Share verify error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "get_prediction")
def handle_prediction(call):
    try:
        user_id = call.message.chat.id
        
        if not is_member(user_id):
            bot.answer_callback_query(call.id, "❌ Channel membership required!", show_alert=True)
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
        
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(f"{ROCKET} New Prediction", callback_data="get_prediction"))
        bot.send_message(user_id, prediction_msg, reply_markup=markup, parse_mode="Markdown")
        cooldowns[user_id] = time.time() + COOLDOWN_SECONDS
        bot.answer_callback_query(call.id, "✅ Prediction generated!")
        
    except Exception as e:
        logger.error(f"Prediction error: {e}")

# Health check endpoint
from flask import Flask, jsonify
app = Flask(__name__)
@app.route('/')
def health_check():
    return jsonify({"status": "ok", "time": str(get_indian_time())})

def run_flask():
    app.run(host='0.0.0.0', port=10000)

if __name__ == '__main__':
    logger.info("🤖 Starting bot...")
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    while True:
        try:
            bot.infinity_polling()
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            time.sleep(10)
            logger.info("Restarting bot...")
