import os, logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ.get("TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "devsecret")
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE") or os.environ.get("RENDER_EXTERNAL_URL")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot online. Webhook mode.")

async def on_startup(app: Application):
    # опционально шлём стартовое уведомление
    if CHAT_ID:
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text="🔔 Webhook bot on Render: startup OK")
        except Exception as e:
            logger.exception("Failed to send startup message: %s", e)

def main():
    if not TOKEN:
        raise RuntimeError("No TOKEN env var provided")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("start", start_cmd))

    # Собираем URL вебхука, если Render уже выдал публичный адрес
    webhook_url = None
    if WEBHOOK_BASE:
        webhook_url = WEBHOOK_BASE.rstrip("/") + f"/webhook/{WEBHOOK_SECRET}"
        logger.info("Using webhook_url: %s", webhook_url)
    else:
        logger.warning("WEBHOOK_BASE/RENDER_EXTERNAL_URL not set yet. Set it or redeploy after URL appears.")

    # В 21.x нужно использовать url_path вместо webhook_path
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=f"/webhook/{WEBHOOK_SECRET}",
        webhook_url=webhook_url,          # можно None на первом запуске; задастся при следующем
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()

