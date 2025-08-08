import os
import logging
import asyncio
from telegram.ext import Application, CommandHandler
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

TOKEN         = os.getenv("TOKEN")
CHAT_ID       = int(os.getenv("CHAT_ID", "0"))
WEBHOOK_BASE  = os.getenv("WEBHOOK_BASE")              # например: https://telegram-bot-webhook-xxxx.onrender.com
WEBHOOK_SECRET= os.getenv("WEBHOOK_SECRET", "secret")
PORT          = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH  = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL   = (WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH) if WEBHOOK_BASE else None

async def start_cmd(update, context):
    await update.message.reply_text("I'm alive 🤖")

async def post_init(app: Application):
    # запускаем фоновый сканер
    if CHAT_ID:
        app.bot_data["scanner_task"] = asyncio.create_task(scanner_loop(app.bot, CHAT_ID))
        logging.info("scanner_task started")
    else:
        logging.warning("CHAT_ID is not set; scanner won't start")

async def post_shutdown(app: Application):
    # корректно останавливаем сканер
    task = app.bot_data.get("scanner_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logging.info("scanner_task cancelled cleanly")

def main():
    if not TOKEN:
        raise RuntimeError("TOKEN is required")
    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_BASE is required")

    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start_cmd))

    # поднимаем вебхук-сервер на $PORT
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
