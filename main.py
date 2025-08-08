# main.py — webhook + scanner в одном процессе, без секрета (для диагностики)
import os, logging, asyncio
from contextlib import suppress
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
from telegram.error import TelegramError
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("web+scanner")

TOKEN = os.getenv("TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE")              # напр.: https://telegram-bot-webhook-xxxx.onrender.com
PORT = int(os.getenv("PORT", "10000"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
# ВАЖНО: секрет убираем на время диагностики
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # допускаем пустую строку

if not TOKEN:       raise RuntimeError("TOKEN is required")
if not WEBHOOK_BASE:raise RuntimeError("WEBHOOK_BASE is required")
if not CHAT_ID:     raise RuntimeError("CHAT_ID is required")

WEBHOOK_PATH = "/webhook" if not WEBHOOK_SECRET else f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH

# --- Handlers ---
async def cmd_start(update, context):
    await update.message.reply_text("Webhook OK ✅")

async def cmd_ping(update, context):
    await update.message.reply_text("pong")

async def any_text(update, context):
    await update.message.reply_text("✅ got it")

# --- Lifecycle ---
async def post_init(app: Application):
    # 1) до установки вебхука — лог
    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook BEFORE set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(before) failed: %s", e)

    # 2) ЯВНО выставляем вебхук (без секрета, чтобы исключить мисматч)
    try:
        await app.bot.set_webhook(
            url=WEBHOOK_URL,
            # secret_token=None  # не передаём вовсе
            allowed_updates=["message","callback_query","my_chat_member"],
            drop_pending_updates=True,
        )
        log.info("set_webhook OK -> %s", WEBHOOK_URL)
    except TelegramError as e:
        log.error("set_webhook FAILED: %s", e)

    # 3) после установки — лог
    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook AFTER set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(after) failed: %s", e)

    # 4) запускаем сканер
    task = app.create_task(scanner_loop(app.bot, CHAT_ID))
    app.bot_data["scanner_task"] = task
    log.info("scanner_task started from web-service")

async def post_shutdown(app: Application):
    task = app.bot_data.get("scanner_task")
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        log.info("scanner_task cancelled cleanly")

def main():
    req = HTTPXRequest(connect_timeout=10, read_timeout=30)
    app = Application.builder().token(TOKEN).request(req).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_text))
    app.add_handler(MessageHandler(filters.ALL, lambda *_: None))

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    log.info("Starting webhook on port %d; path=%s url=%s", PORT, WEBHOOK_PATH, WEBHOOK_URL)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        # secret_token НЕ передаём, чтобы не требовать заголовок от Telegram
        drop_pending_updates=True,
        allowed_updates=["message","callback_query","my_chat_member"],
    )

if __name__ == "__main__":
    main()
