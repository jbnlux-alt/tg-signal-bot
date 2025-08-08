import os, logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("webhook")

TOKEN = os.getenv("TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")
PORT = int(os.getenv("PORT", "10000"))

if not TOKEN: raise RuntimeError("TOKEN is required")
if not WEBHOOK_BASE: raise RuntimeError("WEBHOOK_BASE is required")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH

async def cmd_start(update, context): await update.message.reply_text("Webhook OK ✅")
async def cmd_ping(update, context):  await update.message.reply_text("pong")
async def swallow(update, context):   pass

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(MessageHandler(filters.ALL, swallow))
    log.info("Starting webhook on port %d; path=%s", PORT, WEBHOOK_PATH)
    app.run_webhook(
        listen="0.0.0.0", port=PORT, url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
