import os
import csv
import pytz
import logging
import asyncio
from datetime import datetime, time, timedelta
from threading import Lock

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# --- Load env ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN or TOKEN == "REPLACE_ME_IN_ENV":
    raise RuntimeError("BOT_TOKEN env var not set.")

PORT = int(os.environ.get("PORT", 8000))
TH_TZ = pytz.timezone("Asia/Bangkok")

# --- Work Configuration ---
START_WORK_DEADLINE = time(11, 0)
WORK_END_TIME = time(23, 59)

MAX_TOILET_BREAKS = 6
TOILET_LIMIT_SECONDS = 10 * 60
TOILET_WARNING_SECONDS = 9 * 60

EAT_BREAK_START = time(22, 0)
EAT_BREAK_END = time(22, 30)
EAT_WARNING_BEFORE_END = 60

REST_BREAK_START = time(16, 15)
REST_BREAK_END = time(17, 45)
REST_WARNING_BEFORE_END = 10 * 60

CSV_FILE = "work_log.csv"
CSV_HEADER = ["Timestamp", "UserID", "Username", "Action"]
log_lock = Lock()
user_states = {}
user_states_lock = Lock()
last_auto_off_date = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CSV Functions ---
def setup_csv_file():
    with log_lock:
        if not os.path.exists(CSV_FILE):
            with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADER)

def log_to_csv(user_id, username, action):
    with log_lock:
        try:
            with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                timestamp = datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M:%S")
                writer.writerow([timestamp, user_id, username, action])
        except Exception as e:
            logger.error(f"CSV write error: {e}")

# --- Helpers ---
def get_reply_keyboard():
    return ReplyKeyboardMarkup(
        [["Start Work", "Off Work"], ["Toilet", "Eat", "Rest"], ["Back to Seat"]],
        resize_keyboard=True,
    )

def format_message(username, user_id, action, toilet_count=None, warning=None):
    line = "-" * 60
    msg = f"User: {username}\nID:{user_id}\n{line}\nAction: {action}\n"
    if toilet_count is not None:
        msg += f"{line}\nToilet Count: {toilet_count}/{MAX_TOILET_BREAKS}\n"
    if warning:
        msg += f"{line}\n⚠ {warning}"
    return msg

def format_timedelta(td: timedelta) -> str:
    total_seconds = int(td.total_seconds())
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"

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
                "eat_time": timedelta(),
                "rest_time": timedelta(),
                "toilet_start_time": None,
                "eat_start_time": None,
                "rest_start_time": None,
                "start_work_time": None,
                "username": None,
            }

def cancel_user_jobs(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    for prefix in ["toilet", "eat", "rest"]:
        for suffix in ["warning", "timeout"]:
            name = f"{prefix}_{suffix}_{user_id}"
            for job in context.job_queue.get_jobs_by_name(name):
                job.schedule_removal()

def is_between(now_time: time, start: time, end: time) -> bool:
    return start <= now_time <= end

async def send_scheduled_alert(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(job.chat_id, job.data["message"])

# --- Final Report ---
def generate_final_report(user_id, username, state, now_dt):
    total_toilet_time = state.get("toilet_time", timedelta())
    total_eat_time = state.get("eat_time", timedelta())
    total_rest_time = state.get("rest_time", timedelta())
    total_break_time = total_toilet_time + total_eat_time + total_rest_time
    start_time = state.get("start_work_time") or now_dt
    total_work_time = now_dt - start_time
    pure_work_time = total_work_time - total_break_time

    msg = (
        f"User: {username}\nID:{user_id}\n"
        f"Work Check-Out at {now_dt.strftime('%H:%M:%S')}\n"
        f"Total Work: {format_timedelta(total_work_time)}\n"
        f"Pure Work: {format_timedelta(pure_work_time)}\n"
        f"Total Break: {format_timedelta(total_break_time)}\n"
        f"Eat: {state.get('eat_count')} times ({format_timedelta(total_eat_time)})\n"
        f"Toilet: {state.get('toilet_count')} times ({format_timedelta(total_toilet_time)})\n"
        f"Rest: {state.get('rest_count')} times ({format_timedelta(total_rest_time)})"
    )
    return msg

# --- Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name
    ensure_state(user_id)
    user_states[user_id]['username'] = username
    await update.message.reply_text(
        format_message(username, user_id, "Bot started 🚀"),
        reply_markup=get_reply_keyboard(),
    )
    log_to_csv(user_id, username, "Started bot")

async def back_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update.message.text = "Back to Seat"
    await handle_text(update, context)

# --- Main Message Handler ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name
    ensure_state(user_id)
    now_dt = datetime.now(TH_TZ)

    with user_states_lock:
        state = user_states[user_id]
        state['username'] = username

    text = (update.message.text or "").strip().lower()

    async def send_response(action: str, warning: str = None):
        message_text = format_message(
            username, user_id, action,
            toilet_count=state.get("toilet_count"),
            warning=warning
        )
        await update.message.reply_text(message_text, reply_markup=get_reply_keyboard())
        log_to_csv(user_id, username, action)

    # --- START WORK ---
    if text == "start work":
        with user_states_lock:
            if state["work_started"]:
                await send_response("⚠ Already started work")
                return
            state["work_started"] = True
            state["start_work_time"] = now_dt

        official_start = TH_TZ.localize(datetime.combine(now_dt.date(), START_WORK_DEADLINE))
        if now_dt <= official_start:
            hint = "✅ On time"
        else:
            late_delta = now_dt - official_start
            h, rem = divmod(int(late_delta.total_seconds()), 3600)
            m, s = divmod(rem, 60)
            hint = f"⚠ Late by {h}h {m}m {s}s"

        await send_response(f"✅ Started Work – {hint}")
        log_to_csv(user_id, username, "Start Work")
        return

    # --- OFF WORK ---
    if text == "off work":
        if now_dt.time() < WORK_END_TIME:
            with user_states_lock:
                state["awaiting_offwork_confirmation"] = True
            await update.message.reply_text(
                "⚠ Working hours not over. Off work anyway?",
                reply_markup=ReplyKeyboardMarkup([["Yes", "No"]], resize_keyboard=True),
            )
            return
        msg = generate_final_report(user_id, username, state, now_dt)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_reply_keyboard())
        log_to_csv(user_id, username, "Off Work")
        with user_states_lock:
            user_states.pop(user_id, None)
        return

    # --- Confirmation for early off work ---
    if text == "yes" and state.get("awaiting_offwork_confirmation"):
        msg = generate_final_report(user_id, username, state, now_dt)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_reply_keyboard())
        log_to_csv(user_id, username, "Off Work (confirmed)")
        with user_states_lock:
            user_states.pop(user_id, None)
        return
    if text == "no" and state.get("awaiting_offwork_confirmation"):
        await send_response("🔄 Resumed Work")
        with user_states_lock:
            state["awaiting_offwork_confirmation"] = False
        return

    # --- TOILET / EAT / REST ---
    if text in ["toilet", "eat", "rest"]:
        if state["in_break"]:
            await send_response("🚫 Already in a break")
            return

        with user_states_lock:
            state["in_break"] = True
            state["last_activity"] = text

        cancel_user_jobs(context, user_id)

        if text == "toilet":
            if state["toilet_count"] >= MAX_TOILET_BREAKS:
                await send_response("🚫 Toilet limit reached")
                state["in_break"] = False
                state["last_activity"] = None
                return
            state["toilet_count"] += 1
            state["toilet_start_time"] = now_dt

            context.job_queue.run_once(
                send_scheduled_alert,
                when=TOILET_WARNING_SECONDS,
                chat_id=user_id,
                data={"message": "⏳ Less than 1 min left for Toilet. Please Back to Seat!"},
                name=f"toilet_warning_{user_id}"
            )
            context.job_queue.run_once(
                send_scheduled_alert,
                when=TOILET_LIMIT_SECONDS,
                chat_id=user_id,
                data={"message": "⏰ Toilet time exceeded! Back to Seat immediately."},
                name=f"toilet_timeout_{user_id}"
            )

        elif text == "eat":
            if not is_between(now_dt.time(), EAT_BREAK_START, EAT_BREAK_END):
                await send_response("🍽 Not eating time!")
                state["in_break"] = False
                state["last_activity"] = None
                return
            state["eat_count"] += 1
            state["eat_start_time"] = now_dt
            end_dt = TH_TZ.localize(datetime.combine(now_dt.date(), EAT_BREAK_END))
            seconds_to_end = max(0, int((end_dt - now_dt).total_seconds()))

            if seconds_to_end > EAT_WARNING_BEFORE_END:
                context.job_queue.run_once(
                    send_scheduled_alert,
                    when=seconds_to_end - EAT_WARNING_BEFORE_END,
                    chat_id=user_id,
                    data={"message": "⏳ Less than 1 min left for Eating break. Back to Seat!"},
                    name=f"eat_warning_{user_id}"
                )
            context.job_queue.run_once(
                send_scheduled_alert,
                when=seconds_to_end,
                chat_id=user_id,
                data={"message": "⏰ Eating break ended. Back to Seat!"},
                name=f"eat_timeout_{user_id}"
            )

        elif text == "rest":
            if not is_between(now_dt.time(), REST_BREAK_START, REST_BREAK_END):
                await send_response("🛋 Not rest time!")
                state["in_break"] = False
                state["last_activity"] = None
                return
            state["rest_count"] += 1
            state["rest_start_time"] = now_dt
            end_dt = TH_TZ.localize(datetime.combine(now_dt.date(), REST_BREAK_END))
            seconds_to_end = max(0, int((end_dt - now_dt).total_seconds()))

            if seconds_to_end > REST_WARNING_BEFORE_END:
                context.job_queue.run_once(
                    send_scheduled_alert,
                    when=seconds_to_end - REST_WARNING_BEFORE_END,
                    chat_id=user_id,
                    data={"message": "⚠ Less than 10 min left in Rest break. Back to Seat!"},
                    name=f"rest_warning_{user_id}"
                )
            context.job_queue.run_once(
                send_scheduled_alert,
                when=seconds_to_end,
                chat_id=user_id,
                data={"message": "⏰ Rest break ended. Back to Seat!"},
                name=f"rest_timeout_{user_id}"
            )

        await send_response(f"✅ Started {text.capitalize()}")
        log_to_csv(user_id, username, text.capitalize())
        return

    # --- BACK TO SEAT ---
    if text == "back to seat":
        last = state.get("last_activity")
        if last:
            start_key = f"{last}_start_time"
            total_key = f"{last}_time"
            start_time = state.get(start_key)
            if start_time:
                duration = now_dt - start_time
                state[total_key] = state.get(total_key, timedelta()) + duration
            state["in_break"] = False
            state["last_activity"] = None
            state[start_key] = None
            cancel_user_jobs(context, user_id)
            await send_response(f"✅ Back to Seat from {last.capitalize()}")
        else:
            await send_response("ℹ Not in a break")
        return

    # --- Unknown / Fallback ---
    await update.message.reply_text("❓ Unknown command", reply_markup=get_reply_keyboard())


# --- Auto-Offwork Job ---
async def auto_offwork_check(context: ContextTypes.DEFAULT_TYPE):
    global last_auto_off_date
    now_dt = datetime.now(TH_TZ)
    if now_dt.hour == WORK_END_TIME.hour and now_dt.minute == WORK_END_TIME.minute:
        if last_auto_off_date == now_dt.date():
            return
        last_auto_off_date = now_dt.date()
        for uid, state in list(user_states.items()):
            msg = generate_final_report(uid, state.get("username", "Unknown"), state, now_dt)
            try:
                await context.bot.send_message(uid, msg, parse_mode=ParseMode.HTML, reply_markup=get_reply_keyboard())
            except Exception as e:
                logger.error(f"Failed auto-offwork {uid}: {e}")
            log_to_csv(uid, state.get("username", "Unknown"), "Auto Off Work")
            user_states.pop(uid, None)

# --- Main ---
def main():
    # ✅ Build the application (PTB v20+)
    app = ApplicationBuilder().token(TOKEN).build()

    # --- Add Handlers ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("back", back_cmd))  # if you have back_cmd
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # --- Add repeating job for auto-offwork ---
    app.job_queue.run_repeating(auto_offwork_check, interval=60, first=10)

    # --- Start the bot ---
    # For local/dev: use polling
    # app.run_polling()

    # For deployment with Render (webhook)
    URL = os.environ.get("RENDER_EXTERNAL_URL")
    if URL:
        if not URL.startswith("https://"):
            URL = "https://" + URL.lstrip("https://")
        WEBHOOK_URL = f"{URL}/{TOKEN}"
        # Delete old webhook if any
        asyncio.run(app.bot.delete_webhook())
        # Set webhook
        asyncio.run(app.bot.set_webhook(WEBHOOK_URL))
        print(f"🚀 Webhook set at {WEBHOOK_URL}")
        # Start webhook server
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN
        )
    else:
        print("⚠ RENDER_EXTERNAL_URL not set, running in polling mode...")
        app.run_polling()
