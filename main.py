import os, logging, asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from scanner import scanner_loop  # <-- импорт фонового сканера

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
    await update.message.reply_text("Bot online. Webhook + Scanner mode.")

async def on_startup(app: Application):
    # Запускаем фоновый сканер
    if CHAT_ID:
        asyncio.create_task(scanner_loop(app.bot, CHAT_ID))
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text="🔔 Webhook bot: scanner started")
        except Exception as e:
            logger.exception("Failed to send startup message: %s", e)

def main():
    if not TOKEN:
        raise RuntimeError("No TOKEN env var provided")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("start", start_cmd))
    app.post_init = on_startup

    webhook_url = None
    if WEBHOOK_BASE:
        webhook_url = WEBHOOK_BASE.rstrip("/") + f"/webhook/{WEBHOOK_SECRET}"
        logger.info("Using webhook_url: %s", webhook_url)
    else:
        logger.warning("WEBHOOK_BASE/RENDER_EXTERNAL_URL not set yet. Redeploy after URL appears.")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=f"/webhook/{WEBHOOK_SECRET}",
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()

