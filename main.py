import logging
import os
import threading
import nest_asyncio
import pytz
import csv
import json

from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler
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

# --- Dummy web server to satisfy Render free web service ---
# This server runs on a separate thread to keep the service alive.
PORT = int(os.environ.get("PORT", 8000))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_server():
    server = HTTPServer(("", PORT), Handler)
    print(f"🌐 Dummy web server running on port {PORT}")
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# --- Configuration ---
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN or TOKEN == "REPLACE_ME_IN_ENV":
    raise RuntimeError("BOT_TOKEN env var not set. Please set it to your Telegram bot token.")

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

def generate_final_report(user_id, username, state, now_dt):
    total_toilet_count = state.get("toilet_count", 0)
    total_toilet_time = state.get("toilet_time", timedelta())
    total_rest_time = state.get("rest_time", timedelta())
    total_eat_time = state.get("eat_time", timedelta())
    total_break_time = total_toilet_time + total_rest_time + total_eat_time
    start_time = state.get("start_work_time") or now_dt
    end_time = now_dt
    total_work_time = end_time - start_time
    pure_working_time = total_work_time - total_break_time

    msg = (
        f"👤 <b>User:</b> {username}\n"
        f"🆔 <b>User ID:</b> {user_id}\n"
        f"-------------------------------------------------------------\n"
        f"✅ <b>Check-Out:</b> Off Work – {end_time.strftime('%m/%d %H:%M:%S')}\n"
        f"-------------------------------------------------------------\n"
        f"<i>Hint:</i> Today's work time has been settled.\n"
        f"-------------------------------------------------------------\n"
        f"🕒 <b>Total work time:</b> {format_timedelta(total_work_time)}\n"
        f"💼 <b>Pure work time:</b> {format_timedelta(pure_working_time)}\n"
        f"⏱ <b>Total break time:</b> {format_timedelta(total_break_time)}\n"
        f"-------------------------------------------------------------\n"
        f"🍽 <b>Eat count:</b> {state.get('eat_count', 0)} times\n"
        f"🍽 <b>Eat time:</b> {format_timedelta(total_eat_time)}\n"
        f"🚻 <b>Toilet count:</b> {total_toilet_count} times\n"
        f"🚻 <b>Toilet time:</b> {format_timedelta(total_toilet_time)}\n"
        f"🛋 <b>Rest count:</b> {state.get('rest_count', 0)} times\n"
        f"🛋 <b>Rest time:</b> {format_timedelta(total_rest_time)}"
    )
    return msg

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

    if state["in_break"] and text not in ("back to seat", "/back"):
        await send_response("⚠ Please return to seat before starting another activity.")
        return

    if text in {"toilet", "eat", "rest", "off work", "back to seat"} and not state["work_started"]:
        await update.message.reply_text(
            "⚠ You need to start work first before using this option.",
            reply_markup=get_reply_keyboard()
        )
        return

    # --- START WORK ---
    if text == "start work":
        with user_states_lock:
            if state["work_started"]:
                await send_response("⚠ You have already started work.")
                return
            state["work_started"] = True
            state["start_work_time"] = now_dt

        official_start = TH_TZ.localize(datetime.combine(now_dt.date(), START_WORK_DEADLINE))

        if now_dt <= official_start:
            checkin_status = "✅ Check-In Succeeded"
            start_info = f"Start Work – {now_dt.strftime('%m/%d %H:%M:%S')}"
            hint = "Hint: Remember to check in when Off Work arrives."
        else:
            late_delta = now_dt - official_start
            h, rem = divmod(int(late_delta.total_seconds()), 3600)
            m, s = divmod(rem, 60)
            checkin_status = "⚠ Late Start"
            start_info = f"Started at {now_dt.strftime('%m/%d %H:%M:%S')}"
            hint = f"⏰ Late by {h}h {m}m {s}s"

        await update.message.reply_text(
            f"<b>User:</b> {username}\n"
            f"<b>User ID:</b> {user_id}\n"
            f"-----------------------------------------------------------------------\n"
            f"<b>✅ You have successfully checked in. Have a productive day!🎉</b>\n"
            f"----------------------------------------------------------------------\n"
            f"<b>{checkin_status}:</b> {start_info}\n"
            f"-------------------------------------------------------------------------\n"
            f"{hint}",
            parse_mode="HTML",
            reply_markup=get_reply_keyboard()
        )
        log_to_csv(user_id, username, "Start Work")
        return

    # --- OFF WORK ---
    if text == "off work":
        now_dt = datetime.now(TH_TZ)
        current_time = now_dt.time()
        if current_time < WORK_END_TIME:
            with user_states_lock:
                state["awaiting_offwork_confirmation"] = True

            await update.message.reply_text(
                "⚠ This is your working hour!\nAre you sure you want to off work?",
                reply_markup=ReplyKeyboardMarkup([["Yes", "No"]], resize_keyboard=True)
            )
            return
        else:
            msg = generate_final_report(user_id, username, state, now_dt)
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_reply_keyboard())
            log_to_csv(user_id, username, "Work completed. Final report generated (auto at end time).")
            with user_states_lock:
                if user_id in user_states:
                    del user_states[user_id]
            return

    # --- Handle Yes/No confirmation if awaiting_offwork_confirmation ---
    if text == "yes" and state.get("awaiting_offwork_confirmation"):
        msg = generate_final_report(user_id, username, state, now_dt)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_reply_keyboard())
        log_to_csv(user_id, username, "Work completed. Final report generated.")
        with user_states_lock:
            if user_id in user_states:
                del user_states[user_id]
        return

    if text == "no" and state.get("awaiting_offwork_confirmation"):
        await send_response("🔄 Resumed work.")
        with user_states_lock:
            state["awaiting_offwork_confirmation"] = False
        return

    # --- TOILET ---
    if text == "toilet":
        if state["in_break"]:
            await send_response("🚫 You are already in a break...")
            return
        if state.get("toilet_count", 0) >= MAX_TOILET_BREAKS:
            await send_response("🚫 Toilet break limit exceeded!")
            return
        with user_states_lock:
            state["toilet_count"] += 1
            state["in_break"] = True
            state["last_activity"] = "toilet"
            state["toilet_start_time"] = now_dt
        cancel_user_jobs(context, user_id)
        context.job_queue.run_once(
            send_scheduled_alert,
            when=TOILET_WARNING_SECONDS,
            chat_id=user_id,
            data={"message": "There is less than 1 minute left for this Toilet. Please Back to Seat!"},
            name=f"toilet_warning_{user_id}"
        )
        context.job_queue.run_once(
            send_scheduled_alert,
            when=TOILET_LIMIT_SECONDS,
            chat_id=user_id,
            data={"message": "⏰ Toilet time exceeded. Please Back to Seat immediately!"},
            name=f"toilet_timeout_{user_id}"
        )
        await update.message.reply_text(
            f"<b>User:</b> {username}\n<b>User ID:</b> {user_id}\n"
            f"-------------------------------------------------------------\n"
            f"<b>✅ Check-In Succeeded:</b> Toilet – {now_dt.strftime('%m/%d %H:%M:%S')}\n"
            f"-------------------------------------------------------------\n"
            f"<b>Attention:</b> This is your {state['toilet_count']} time Toilet.\n"
            f"-------------------------------------------------------------\n"
            f"<b>Time Limit for This Activity:</b> 10 minutes\n"
            f"-------------------------------------------------------------\n"
            f"<b>Tip:</b> Please check in Back to Seat after completing the activity.\n"
            f"----------------------------------------------------------------------\n"
            f"<b>Back to Seat:</b> /back",
            parse_mode=ParseMode.HTML, reply_markup=get_reply_keyboard()
        )
        log_to_csv(user_id, username, "Toilet")
        return

    # --- EAT ---
    if text == "eat":
        if state["in_break"]:
            await send_response("🚫 You are already in a break...")
            return
        if not is_between(now_dt.time(), EAT_BREAK_START, EAT_BREAK_END):
            await send_response("🍽 Attempted eating", warning="This is not your designated eating time.")
            return
        with user_states_lock:
            state["last_activity"] = "eat"
            state["eat_count"] += 1
            state["in_break"] = True
            state["eat_start_time"] = now_dt
        end_dt = TH_TZ.localize(datetime.combine(now_dt.date(), EAT_BREAK_END))
        seconds_to_end = max(0, int((end_dt - now_dt).total_seconds()))
        cancel_user_jobs(context, user_id)
        if seconds_to_end > EAT_WARNING_BEFORE_END:
            context.job_queue.run_once(
                send_scheduled_alert,
                when=seconds_to_end - EAT_WARNING_BEFORE_END,
                chat_id=user_id,
                data={"message": "⏳ Less than 1 minute left in your Eating Break. Please return to your seat!"},
                name=f"eat_warning_{user_id}"
            )
        context.job_queue.run_once(
            send_scheduled_alert,
            when=seconds_to_end,
            chat_id=user_id,
            data={"message": "🍽⏰ Eating break time ended. Please return to your seat!"},
            name=f"eat_timeout_{user_id}"
        )
        warning_msg = "Warning: After this activity, your number of Eat today will reach the upper limit." if state["eat_count"] >= 2 else ""
        await update.message.reply_text(
            f"<b>User:</b> {username}\n<b>User ID:</b> {user_id}\n"
            f"-------------------------------------------------------------\n"
            f"<b>✅ Check-In Succeeded:</b> Eat – {now_dt.strftime('%m/%d %H:%M:%S')}\n"
            f"-------------------------------------------------------------\n"
            f"<b>Attention:</b> This is your {state['eat_count']} time Eat.\n"
            f"-------------------------------------------------------------\n"
            f"<b>Time Window:</b> 22:00–22:30 (ends at {EAT_BREAK_END.strftime('%H:%M')})\n"
            f"-------------------------------------------------------------\n"
            f"{warning_msg}\n"
            f"<b>Tip:</b> Please check in Back to Seat after completing the activity.\n"
            f"---------------------------------------------------------------------\n"
            f"<b>Back to Seat:</b> /back",
            parse_mode=ParseMode.HTML, reply_markup=get_reply_keyboard()
        )
        log_to_csv(user_id, username, "Eat")
        return

    # --- REST ---
    if text == "rest":
        if state["in_break"]:
            await send_response("🚫 You are already in a break...")
            return
        if not is_between(now_dt.time(), REST_BREAK_START, REST_BREAK_END):
            await send_response("🛋 Attempted rest", warning="This is not your designated rest time.")
            return
        with user_states_lock:
            state["last_activity"] = "rest"
            state["rest_count"] += 1
            state["in_break"] = True
            state["rest_start_time"] = now_dt
        end_dt = TH_TZ.localize(datetime.combine(now_dt.date(), REST_BREAK_END))
        seconds_to_end = max(0, int((end_dt - now_dt).total_seconds()))
        cancel_user_jobs(context, user_id)
        if seconds_to_end > REST_WARNING_BEFORE_END:
            context.job_queue.run_once(
                send_scheduled_alert,
                when=seconds_to_end - REST_WARNING_BEFORE_END,
                chat_id=user_id,
                data={"message": "⚠ Less than 10 minutes left in your Rest Break. Please return to your seat!"},
                name=f"rest_warning_{user_id}"
            )
        context.job_queue.run_once(
            send_scheduled_alert,
            when=seconds_to_end,
            chat_id=user_id,
            data={"message": "⏰ Rest break time ended. Please return to your seat!"},
            name=f"rest_timeout_{user_id}"
        )
        await update.message.reply_text(
            f"<b>User:</b> {username}\n<b>User ID:</b> {user_id}\n"
            f"-------------------------------------------------------------\n"
            f"<b>✅ Check-In Succeeded:</b> Rest – {now_dt.strftime('%m/%d %H:%M:%S')}\n"
            f"-------------------------------------------------------------\n"
            f"<b>Attention:</b> This is your {state['rest_count']} time Rest.\n"
            f"-------------------------------------------------------------\n"
            f"<b>Time Window:</b> 14:45–16:15 (ends at {REST_BREAK_END.strftime('%H:%M')})\n"
            f"-------------------------------------------------------------\n"
            f"<b>Tip:</b> Please check in Back to Seat after completing the activity.\n"
            f"---------------------------------------------------------------------\n"
            f"<b>Back to Seat:</b> /back",
            parse_mode=ParseMode.HTML, reply_markup=get_reply_keyboard()
        )
        log_to_csv(user_id, username, "Rest")
        return

    # --- BACK TO SEAT ---
    if text == "back to seat":
        last = state.get("last_activity")
        if last:
            with user_states_lock:
                start_time_key = f"{last}_start_time"
                total_time_key = f"{last}_time"
                end_time = now_dt
                start_time = state.get(start_time_key)
                if start_time:
                    duration = end_time - start_time
                    state[total_time_key] = state.get(total_time_key, timedelta()) + duration
                    late_message = ""

                    if last == "toilet" and duration > timedelta(seconds=TOILET_LIMIT_SECONDS):
                        late_by = duration - timedelta(seconds=TOILET_LIMIT_SECONDS)
                        late_message = f"\n\n<b>🚨 You were late by {format_timedelta(late_by)} to return from Toilet.🚨</b>"

                    elif last == "eat":
                        end_dt = TH_TZ.localize(datetime.combine(end_time.date(), EAT_BREAK_END))
                        diff = end_time - end_dt
                        if diff.total_seconds() > 0:
                            late_message = f"\n\n<b>🚨 You were late by {format_timedelta(diff)} to return from Eat.🚨</b>"
                        else:
                            late_message = f"\n\n<b>✅ You returned {format_timedelta(abs(diff))} early from Eat.</b>"

                    elif last == "rest":
                        end_dt = TH_TZ.localize(datetime.combine(end_time.date(), REST_BREAK_END))
                        diff = end_time - end_dt
                        if diff.total_seconds() > 0:
                            late_message = f"\n\n<b>🚨 You were late by {format_timedelta(diff)} to return from Rest.🚨</b>"
                        else:
                            late_message = f"\n\n<b>✅ You returned {format_timedelta(abs(diff))} early from Rest.</b>"

                else:
                    duration = timedelta()
                state["last_activity"] = None
                state["in_break"] = False
                state[start_time_key] = None

            cancel_user_jobs(context, user_id)
            activity_time_map = {
                "eat": "Total Eat time today",
                "toilet": "Total Toilet time today",
                "rest": "Total Rest time today"
            }
            activity_total_time = state.get(f"{last}_time", timedelta())
            total_all = (
                state.get("toilet_time", timedelta()) +
                state.get("rest_time", timedelta()) +
                state.get("eat_time", timedelta())
            )

            await update.message.reply_text(
                f"<b>User:</b> {username}\n<b>User ID:</b> {user_id}\n"
                f"-------------------------------------------------------------\n"
                f"<b>✅ Back to Seat Check-In Succeeded:</b> {last.capitalize()} – {now_dt.strftime('%m/%d %H:%M:%S')}\n"
                f"-------------------------------------------------------------\n"
                f"<i>Hint:</i> This activity's time has been settled.\n"
                f"-------------------------------------------------------------\n"
                f"<b>Time Used for This Activity:</b> {format_timedelta(duration)}\n"
                f"<b>{activity_time_map.get(last, 'Unknown Activity')}:</b> {format_timedelta(activity_total_time)}\n"
                f"<b>Total time for all activities today:</b> {format_timedelta(total_all)}\n"
                f"-------------------------------------------------------------\n"
                f"<b>Today's Eat:</b> {state.get('eat_count')} times\n"
                f"<b>Today's Toilet:</b> {state.get('toilet_count')} times\n"
                f"<b>Today's Rest:</b> {state.get('rest_count')} times\n"
                f"{late_message}",
                parse_mode=ParseMode.HTML, reply_markup=get_reply_keyboard()
            )
            log_to_csv(user_id, username, "Back to Seat")
        else:
            await send_response("ℹ You weren't on a break to return from.")
        return

    # --- Unknown / fallback ---
    await update.message.reply_text("❓ Unknown command. Use the buttons below.", reply_markup=get_reply_keyboard())
    
# --- Main ---
def main():
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

    print("🚀 Removing old webhook just in case...")
    app.bot.delete_webhook()
    print(f"🚀 Setting new webhook to {WEBHOOK_URL}...")
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
