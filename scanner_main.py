import os, asyncio
from telegram import Bot
from scanner import scanner_loop

# Читает TOKEN/CHAT_ID (как у твоего web-сервиса) или TELEGRAM_TOKEN/ADMIN_CHAT (фолбэк)
TOKEN = os.getenv("TOKEN") or os.getenv("TELEGRAM_TOKEN")
CHAT  = os.getenv("CHAT_ID") or os.getenv("ADMIN_CHAT")

if not TOKEN or not CHAT:
    raise SystemExit("Set TOKEN/CHAT_ID (или TELEGRAM_TOKEN/ADMIN_CHAT) в Render → Environment")

async def main():
    bot = Bot(TOKEN)
    await scanner_loop(bot, int(CHAT))

if __name__ == "__main__":
    asyncio.run(main())
