import logging
import os
import threading
import nest_asyncio
import pytz
import csv

from dotenv import load_dotenv
from datetime import datetime, time, timedelta
from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
load_dotenv()

# --- Configuration ---
TOKEN = os.getenv("BOT_TOKEN")
START_WORK_DEADLINE = time(11, 0)

MAX_TOILET_BREAKS = 6
TOILET_LIMIT_SECONDS = 10 * 60
TOILET_WARNING_SECONDS = 9 * 60

EAT_BREAK_START = time(22, 0)
EAT_BREAK_END = time(22, 30)
EAT_WARNING_BEFORE_END = 60

REST_BREAK_START = time(16, 15)
REST_BREAK_END = time(17, 45)
REST_WARNING_BEFORE_END = 10 * 60

WORK_END_TIME = time(23, 59)

TH_TZ = pytz.timezone("Asia/Bangkok")

CSV_FILE = "work_log.csv"
CSV_HEADER = ["Timestamp", "UserID", "Username", "Action"]
log_lock = threading.Lock()

user_states = {}
user_states_lock = threading.Lock()
last_auto_off_date = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CSV Functions ---
def setup_csv_file():
    with log_lock:
        if not os.path.exists(CSV_FILE):
            try:
                with open(CSV_FILE, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(CSV_HEADER)
            except IOError as e:
                logger.error(f"Error creating CSV file: {e}")

def log_to_csv(user_id, username, action):
    with log_lock:
        try:
            with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                timestamp = datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')
                writer.writerow([timestamp, user_id, username, action])
        except IOError as e:
            logger.error(f"Error writing to CSV file: {e}")

# --- Helpers ---
def get_reply_keyboard():
    return ReplyKeyboardMarkup(
        [["Start Work", "Off Work"],
         ["Toilet", "Eat", "Rest"],
         ["Back to Seat"]],
        resize_keyboard=True
    )

def format_message(username, user_id, action, toilet_count=None, warning=None):
    line = "----------------------------------------------------------"
    msg = (
        f"User: {username}\n"
        f"🆔 ID:{user_id}\n"
        f"{line}\n"
        f"📍Action: {action}\n"
    )
    if toilet_count is not None:
        msg += f"{line}\n 🚽 Toilet Count: {toilet_count}/{MAX_TOILET_BREAKS}\n"
    msg += f"{line}\n"
    if warning:
        msg += f"⚠ {warning}"
    return msg

def format_timedelta(td: timedelta) -> str:
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"

def ensure_state(user_id: int):
    with user_states_lock:
        if user_id not in user_states:
            user_states[user_id] = {
                "work_started": False,
                "in_break": False,
                "toilet_count": 0,
                "eat_count": 0,
                "rest_count": 0,
                "last_activity": None,
                "awaiting_offwork_confirmation": False,
                "toilet_time": timedelta(),
                "rest_time": timedelta(),
                "eat_time": timedelta(),
                "toilet_start_time": None,
                "rest_start_time": None,
                "eat_start_time": None,
                "start_work_time": None,
                "username": None,
            }

def cancel_user_jobs(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    prefixes = [
        "toilet_warning", "toilet_timeout",
        "eat_warning", "eat_timeout",
        "rest_warning", "rest_timeout",
    ]
    for p in prefixes:
        for job in context.job_queue.get_jobs_by_name(f"{p}_{user_id}"):
            job.schedule_removal()

def is_between(now_time: time, start: time, end: time) -> bool:
    return start <= now_time <= end

async def send_scheduled_alert(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(job.chat_id, job.data["message"])

# --- Auto-Offwork checker ---
async def auto_offwork_check(context: ContextTypes.DEFAULT_TYPE):
    global last_auto_off_date
    now_dt = datetime.now(TH_TZ)
    if now_dt.hour == WORK_END_TIME.hour and now_dt.minute == WORK_END_TIME.minute:
        if last_auto_off_date == now_dt.date():
            return
        last_auto_off_date = now_dt.date()

        to_close = []
        with user_states_lock:
            for uid, st in user_states.items():
                if st.get("work_started", False):
                    to_close.append(uid)

        for uid in to_close:
            with user_states_lock:
                state = user_states.get(uid)
                if not state:
                    continue
                username = state.get("username") or "Unknown"

            msg = generate_final_report(uid, username, state, now_dt)
            try:
                await context.bot.send_message(uid, msg, parse_mode=ParseMode.HTML, reply_markup=get_reply_keyboard())
            except Exception as e:
                logger.error(f"Failed sending auto-offwork report to {uid}: {e}")

            log_to_csv(uid, username, "Work completed (auto at 23:59). Final report generated.")
            with user_states_lock:
                if uid in user_states:
                    del user_states[uid]

# --- Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name
    ensure_state(user_id)
    with user_states_lock:
        user_states[user_id]['username'] = username
    await update.message.reply_text(
        format_message(username, user_id, "Started bot 🚀"),
        reply_markup=get_reply_keyboard()
    )
    log_to_csv(user_id, username, "Started bot")

async def back_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update.message.text = "Back to Seat"
    await handle_text(update, context)

# --- Message Handler ---
# (keep your existing handle_text function here without changes)

# --- Final report generator ---
# (keep your existing generate_final_report function here without changes)

# --- Main ---
def main():
    if not TOKEN or TOKEN == "REPLACE_ME_IN_ENV":
        raise RuntimeError("BOT_TOKEN env var not set. Please set it to your Telegram bot token.")
    
    setup_csv_file()

    app = ApplicationBuilder().token(TOKEN).build()
    job_queue = app.job_queue

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("back", back_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    job_queue.run_repeating(auto_offwork_check, interval=60, first=10, name="auto_offwork_checker")

    PORT = int(os.environ.get("PORT", 8000))
    URL = os.environ.get("RENDER_EXTERNAL_URL")
    WEBHOOK_PATH = f"/{TOKEN}"
    WEBHOOK_URL = f"{URL}{WEBHOOK_PATH}"

    # Remove old webhook just in case
    app.bot.delete_webhook()
    # Set new webhook
    app.bot.set_webhook(WEBHOOK_URL)

    print(f"🚀 Bot is running with webhook at {WEBHOOK_URL} on port {PORT}!")

    nest_asyncio.apply()
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
    )

if __name__ == "__main__":
    main()
