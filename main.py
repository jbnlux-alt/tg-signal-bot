
import os, logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ.get("TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "c9d66e2eafb9e28e76d4454451c1953a")
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE") or os.environ.get("RENDER_EXTERNAL_URL")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot online. Webhook mode.")

async def on_startup(app: Application):
    if WEBHOOK_BASE:
        url = WEBHOOK_BASE.rstrip("/") + f"/webhook/{WEBHOOK_SECRET}".format(WEBHOOK_SECRET=WEBHOOK_SECRET)
        await app.bot.set_webhook(url=url, secret_token=WEBHOOK_SECRET, allowed_updates=Update.ALL_TYPES)
        logger.info("Webhook set to %s", url)
    else:
        logger.warning("WEBHOOK_BASE/RENDER_EXTERNAL_URL not set; set and redeploy to enable webhook.")
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
    app.post_init = on_startup

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_path=f"/webhook/{WEBHOOK_SECRET}".format(WEBHOOK_SECRET=WEBHOOK_SECRET),
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
