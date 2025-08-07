import os, logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ.get("TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))  # 1733067489

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def on_startup(app: Application):
    if CHAT_ID:
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text="🔔 Minimal bot on Render: startup OK")
            logger.info("Startup message sent.")
        except Exception as e:
            logger.exception("Send failed: %s", e)

def main():
    if not TOKEN:
        raise RuntimeError("No TOKEN env var provided")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("ping", ping))
    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
