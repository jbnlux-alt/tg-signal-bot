# main.py — webhook + scanner в одном процессе (для плана без worker)
import os, logging, asyncio
from contextlib import suppress
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("web+scanner")

TOKEN = os.getenv("TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")
PORT = int(os.getenv("PORT", "10000"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))

if not TOKEN:       raise RuntimeError("TOKEN is required")
if not WEBHOOK_BASE:raise RuntimeError("WEBHOOK_BASE is required")
if not CHAT_ID:     raise RuntimeError("CHAT_ID is required")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH

async def cmd_start(update, context): await update.message.reply_text("Webhook OK ✅")
async def cmd_ping(update, context):  await update.message.reply_text("pong")
async def swallow(update, context):   pass

async def post_init(app):
    # запускаем сканер как фоновую задачу PTB (устойчиво к сетевым глюкам)
    task = app.create_task(scanner_loop(app.bot, CHAT_ID))
    app.bot_data["scanner_task"] = task
    log.info("scanner_task started from web-service")

async def post_shutdown(app):
    # аккуратно гасим сканер при остановке веб-сервиса
    task = app.bot_data.get("scanner_task")
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        log.info("scanner_task cancelled cleanly")

def main():
    req = HTTPXRequest(connect_timeout=10, read_timeout=30)  # телега иногда тыркается на старте
    app = Application.builder().token(TOKEN).request(req).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(MessageHandler(filters.ALL, swallow))

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    log.info("Starting webhook on port %d; path=%s", PORT, WEBHOOK_PATH)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
