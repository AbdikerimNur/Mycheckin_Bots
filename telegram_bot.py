# telegram_bot.py

import os
import json
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from telegram import (
    Update,
    constants,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from scheduler_service import SchedulerService  # uses async JobQueue-based scheduler

# ========= ENV & GLOBALS =========
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
USERS_FILE = "users.json"
CST_TZ = ZoneInfo("America/Chicago")

# Conversation states
LOGIN_USERNAME, LOGIN_PASSWORD, ASK_REUSE, SET_START, SET_END = range(5)


# ========= PERSISTENCE =========
def load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_users(data: dict) -> None:
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=2)


USERS = load_users()


# ========= UTILS =========
def now_stamp() -> str:
    return datetime.now(CST_TZ).strftime("[%I:%M:%S %p %Z]")


async def _send(app, chat_id, text):
    print(text)
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        print(f"[Telegram SendError] {e}")


# ========= COMMANDS =========
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to the Automated Check-In Bot!*\n\n"
        "Here’s how to get rolling:\n"
        "1️⃣ /setlogin – Save your MITC username & password\n"
        "2️⃣ /settime – Set your check-in window (start & end)\n"
        "3️⃣ /startcheckin – Begin automated check-ins (every 30m)\n"
        "4️⃣ /stopcheckin – Cancel today’s check-ins\n"
        "5️⃣ /status – View your current settings",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


# ----- LOGIN -----
async def setlogin(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🪪 Enter your MITC username:")
    return LOGIN_USERNAME


async def got_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["username"] = update.message.text.strip()
    await update.message.reply_text("🔑 Enter your MITC password:")
    return LOGIN_PASSWORD


async def got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    USERS.setdefault(cid, {})["username"] = context.user_data["username"]
    USERS[cid]["password"] = update.message.text.strip()
    save_users(USERS)
    await update.message.reply_text(
        "✅ Credentials saved!\n\nUse /settime to configure your check-in window.",
        parse_mode=constants.ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


# ----- SET TIME -----
async def settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask whether to reuse saved time or set new."""
    cid = str(update.effective_chat.id)
    u = USERS.get(cid, {})
    start_t = u.get("start_time")
    end_t = u.get("end_time")

    if start_t and end_t:
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Yes, use this", callback_data="reuse_yes"),
                    InlineKeyboardButton("✏️ No, set new", callback_data="reuse_no"),
                ]
            ]
        )
        await update.message.reply_text(
            f"🗓️ You have a saved window: *{start_t} → {end_t}*.\n\nDo you want to reuse it?",
            reply_markup=kb,
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return ASK_REUSE

    await update.message.reply_text("🕐 Enter your START time (e.g., 11:30PM):")
    return SET_START


async def on_reuse_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cid = str(update.effective_chat.id)
    choice = query.data

    if choice == "reuse_yes":
        u = USERS.get(cid, {})
        start_t = u.get("start_time", "Not set")
        end_t = u.get("end_time", "Not set")
        await query.edit_message_text(
            f"✅ Reusing saved window: *{start_t} → {end_t}*.\n\nUse /startcheckin to begin.",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    await query.edit_message_text("✏️ Okay — let's set a new window.\n\n🕐 Enter your START time (e.g., 11:30PM):")
    return SET_START


async def got_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["start_time"] = update.message.text.strip().upper().replace(" ", "")
    await update.message.reply_text("🕓 Now enter your END time (e.g., 6:30AM):")
    return SET_END


async def got_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid_int = update.effective_chat.id
    cid = str(cid_int)
    start_t = context.user_data["start_time"]
    end_t = update.message.text.strip().upper().replace(" ", "")

    USERS.setdefault(cid, {}).update({"start_time": start_t, "end_time": end_t})
    save_users(USERS)

    scheduler: SchedulerService = context.application.bot_data["scheduler"]
    cancelled = False
    try:
        cancelled = await scheduler.cancel_jobs(cid, silent=True)
    except TypeError:
        try:
            await scheduler.cancel_jobs(cid)
            cancelled = True
        except Exception:
            cancelled = False

    extra = "\n🧹 Replaced your previous schedule." if cancelled else ""
    await update.message.reply_text(
        f"✅ Schedule set for *{start_t} → {end_t}*.\nUse /startcheckin to begin.{extra}",
        parse_mode=constants.ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


# ----- RUN / STOP / STATUS -----
async def startcheckin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid_int = update.effective_chat.id
    cid = str(cid_int)
    u = USERS.get(cid)
    if not u or not all(k in u for k in ("username", "password", "start_time", "end_time")):
        await update.message.reply_text("⚠️ Use /setlogin and /settime first.")
        return

    scheduler: SchedulerService = context.application.bot_data["scheduler"]
    if scheduler.has_active_job(cid):
        await update.message.reply_text("ℹ️ Check-ins already active. Use /stopcheckin first or /settime to replace.")
        return

    await scheduler.schedule_user(cid, u["username"], u["password"], u["start_time"], u["end_time"])
    await update.message.reply_text("✅ Automated check-ins scheduled.")


async def stopcheckin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    scheduler: SchedulerService = context.application.bot_data["scheduler"]
    try:
        await scheduler.cancel_jobs(cid)
    except Exception:
        pass
    await update.message.reply_text("🛑 Sent cancellation request.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    u = USERS.get(cid)
    if not u:
        await update.message.reply_text("⚠️ No saved info. Use /setlogin.")
        return

    now = datetime.now(CST_TZ).strftime("%I:%M %p %Z")
    scheduler: SchedulerService = context.application.bot_data["scheduler"]
    active_info = scheduler.get_active_job_info(cid)

    status_text = (
        f"👤 *User:* `{u.get('username', 'Not set')}`\n"
        f"🕒 *Window:* `{u.get('start_time','Not set')}` → `{u.get('end_time','Not set')}`\n\n"
        f"⌚ *Current Time:* `{now}`\n\n"
    )

    if active_info:
        w_start, w_end = active_info["window"]
        status_text += f"✅ *Status:* **ACTIVE**\n🗓️ Running from `{w_start}` to `{w_end}`."
    else:
        status_text += "🛑 *Status:* **INACTIVE**\nUse /startcheckin to begin."

    await update.message.reply_text(status_text, parse_mode=constants.ParseMode.MARKDOWN)


async def cancel(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


async def unknown(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("🤖 Sorry, I didn’t recognize that command.")


# ========= STARTUP =========
async def post_startup(app: Application):
    scheduler = SchedulerService()
    scheduler.set_app(app)
    app.bot_data["scheduler"] = scheduler
    print("♻️ Telegram loop confirmed. Scheduler activation unlocked.")


# ========= MAIN =========
def main():
    print("🧭 Scheduler loop active...")

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .concurrent_updates(True)
        .post_init(post_startup)
        .build()
    )

    # /setlogin conversation
    conv_login = ConversationHandler(
        entry_points=[CommandHandler("setlogin", setlogin)],
        states={
            LOGIN_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_username)],
            LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        name="login_conv",
        persistent=False,
    )

    # /settime conversation (warning-free)
    conv_time = ConversationHandler(
        entry_points=[CommandHandler("settime", settime)],
        states={
            ASK_REUSE: [CallbackQueryHandler(on_reuse_choice, pattern="^(reuse_yes|reuse_no)$")],
            SET_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_start)],
            SET_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_end)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        # per_message removed ✅
        name="time_conv",
        persistent=False,
    )

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_login)
    app.add_handler(conv_time)
    app.add_handler(CommandHandler("startcheckin", startcheckin))
    app.add_handler(CommandHandler("stopcheckin", stopcheckin))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("✅ Bot is running. Scheduler will start once polling begins.")
    app.run_polling(stop_signals=None)
    print("🛑 Bot stopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("🛑 Bot stopped manually.")
