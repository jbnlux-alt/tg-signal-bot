# main.py — webhook + scanner в одном процессе (план без worker)
import os, logging, asyncio
from contextlib import suppress
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest          # ← вот этот импорт
from telegram.error import TelegramError
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("web+scanner")

TOKEN = os.getenv("TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE")              # напр.: https://telegram-bot-webhook-xxxx.onrender.com
PORT = int(os.getenv("PORT", "10000"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # можно пустым

if not TOKEN:       raise RuntimeError("TOKEN is required")
if not WEBHOOK_BASE:raise RuntimeError("WEBHOOK_BASE is required")
if not CHAT_ID:     raise RuntimeError("CHAT_ID is required")

WEBHOOK_PATH = "/webhook" if not WEBHOOK_SECRET else f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH

async def cmd_start(update, context): await update.message.reply_text("Webhook OK ✅")
async def cmd_ping(update, context):  await update.message.reply_text("pong")
async def any_text(update, context):  await update.message.reply_text("✅ got it")

async def post_init(app: Application):
    # Логируем/выставляем вебхук (удобно для диагностики)
    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook BEFORE set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(before) failed: %s", e)

    try:
        await app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=["message","callback_query","my_chat_member"],
            drop_pending_updates=True,
        )
        log.info("set_webhook OK -> %s", WEBHOOK_URL)
    except TelegramError as e:
        log.error("set_webhook FAILED: %s", e)

    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook AFTER set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(after) failed: %s", e)

    # Запускаем сканер в фоне
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
    # ← вот эти ДВЕ строки тебе и нужны
    req = HTTPXRequest(connect_timeout=10, read_timeout=45, write_timeout=30)
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
        drop_pending_updates=True,
        allowed_updates=["message","callback_query","my_chat_member"],
    )

if __name__ == "__main__":
    main()
# main.py — webhook + scanner в одном процессе (план без worker)
import os, logging, asyncio
from contextlib import suppress
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest          # ← вот этот импорт
from telegram.error import TelegramError
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("web+scanner")

TOKEN = os.getenv("TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE")              # напр.: https://telegram-bot-webhook-xxxx.onrender.com
PORT = int(os.getenv("PORT", "10000"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # можно пустым

if not TOKEN:       raise RuntimeError("TOKEN is required")
if not WEBHOOK_BASE:raise RuntimeError("WEBHOOK_BASE is required")
if not CHAT_ID:     raise RuntimeError("CHAT_ID is required")

WEBHOOK_PATH = "/webhook" if not WEBHOOK_SECRET else f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH

async def cmd_start(update, context): await update.message.reply_text("Webhook OK ✅")
async def cmd_ping(update, context):  await update.message.reply_text("pong")
async def any_text(update, context):  await update.message.reply_text("✅ got it")

async def post_init(app: Application):
    # Логируем/выставляем вебхук (удобно для диагностики)
    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook BEFORE set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(before) failed: %s", e)

    try:
        await app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=["message","callback_query","my_chat_member"],
            drop_pending_updates=True,
        )
        log.info("set_webhook OK -> %s", WEBHOOK_URL)
    except TelegramError as e:
        log.error("set_webhook FAILED: %s", e)

    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook AFTER set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(after) failed: %s", e)

    # Запускаем сканер в фоне
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
    # ← вот эти ДВЕ строки тебе и нужны
    req = HTTPXRequest(connect_timeout=10, read_timeout=45, write_timeout=30)
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
        drop_pending_updates=True,
        allowed_updates=["message","callback_query","my_chat_member"],
    )

if __name__ == "__main__":
    main()
# main.py — webhook + scanner в одном процессе (план без worker)
import os, logging, asyncio
from contextlib import suppress
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest          # ← вот этот импорт
from telegram.error import TelegramError
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("web+scanner")

TOKEN = os.getenv("TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE")              # напр.: https://telegram-bot-webhook-xxxx.onrender.com
PORT = int(os.getenv("PORT", "10000"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # можно пустым

if not TOKEN:       raise RuntimeError("TOKEN is required")
if not WEBHOOK_BASE:raise RuntimeError("WEBHOOK_BASE is required")
if not CHAT_ID:     raise RuntimeError("CHAT_ID is required")

WEBHOOK_PATH = "/webhook" if not WEBHOOK_SECRET else f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH

async def cmd_start(update, context): await update.message.reply_text("Webhook OK ✅")
async def cmd_ping(update, context):  await update.message.reply_text("pong")
async def any_text(update, context):  await update.message.reply_text("✅ got it")

async def post_init(app: Application):
    # Логируем/выставляем вебхук (удобно для диагностики)
    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook BEFORE set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(before) failed: %s", e)

    try:
        await app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=["message","callback_query","my_chat_member"],
            drop_pending_updates=True,
        )
        log.info("set_webhook OK -> %s", WEBHOOK_URL)
    except TelegramError as e:
        log.error("set_webhook FAILED: %s", e)

    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook AFTER set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(after) failed: %s", e)

    # Запускаем сканер в фоне
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
    # ← вот эти ДВЕ строки тебе и нужны
    req = HTTPXRequest(connect_timeout=10, read_timeout=45, write_timeout=30)
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
        drop_pending_updates=True,
        allowed_updates=["message","callback_query","my_chat_member"],
    )

if __name__ == "__main__":
    main()
# main.py — webhook + scanner в одном процессе (план без worker)
import os, logging, asyncio
from contextlib import suppress
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest          # ← вот этот импорт
from telegram.error import TelegramError
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("web+scanner")

TOKEN = os.getenv("TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE")              # напр.: https://telegram-bot-webhook-xxxx.onrender.com
PORT = int(os.getenv("PORT", "10000"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # можно пустым

if not TOKEN:       raise RuntimeError("TOKEN is required")
if not WEBHOOK_BASE:raise RuntimeError("WEBHOOK_BASE is required")
if not CHAT_ID:     raise RuntimeError("CHAT_ID is required")

WEBHOOK_PATH = "/webhook" if not WEBHOOK_SECRET else f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH

async def cmd_start(update, context): await update.message.reply_text("Webhook OK ✅")
async def cmd_ping(update, context):  await update.message.reply_text("pong")
async def any_text(update, context):  await update.message.reply_text("✅ got it")

async def post_init(app: Application):
    # Логируем/выставляем вебхук (удобно для диагностики)
    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook BEFORE set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(before) failed: %s", e)

    try:
        await app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=["message","callback_query","my_chat_member"],
            drop_pending_updates=True,
        )
        log.info("set_webhook OK -> %s", WEBHOOK_URL)
    except TelegramError as e:
        log.error("set_webhook FAILED: %s", e)

    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook AFTER set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(after) failed: %s", e)

    # Запускаем сканер в фоне
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
    # ← вот эти ДВЕ строки тебе и нужны
    req = HTTPXRequest(connect_timeout=10, read_timeout=45, write_timeout=30)
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
        drop_pending_updates=True,
        allowed_updates=["message","callback_query","my_chat_member"],
    )

if __name__ == "__main__":
    main()
# main.py — webhook + scanner в одном процессе (план без worker)
import os, logging, asyncio
from contextlib import suppress
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest          # ← вот этот импорт
from telegram.error import TelegramError
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("web+scanner")

TOKEN = os.getenv("TOKEN")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE")              # напр.: https://telegram-bot-webhook-xxxx.onrender.com
PORT = int(os.getenv("PORT", "10000"))
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # можно пустым

if not TOKEN:       raise RuntimeError("TOKEN is required")
if not WEBHOOK_BASE:raise RuntimeError("WEBHOOK_BASE is required")
if not CHAT_ID:     raise RuntimeError("CHAT_ID is required")

WEBHOOK_PATH = "/webhook" if not WEBHOOK_SECRET else f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL  = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH

async def cmd_start(update, context): await update.message.reply_text("Webhook OK ✅")
async def cmd_ping(update, context):  await update.message.reply_text("pong")
async def any_text(update, context):  await update.message.reply_text("✅ got it")

async def post_init(app: Application):
    # Логируем/выставляем вебхук (удобно для диагностики)
    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook BEFORE set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(before) failed: %s", e)

    try:
        await app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=["message","callback_query","my_chat_member"],
            drop_pending_updates=True,
        )
        log.info("set_webhook OK -> %s", WEBHOOK_URL)
    except TelegramError as e:
        log.error("set_webhook FAILED: %s", e)

    try:
        info = await app.bot.get_webhook_info()
        log.info("Webhook AFTER set: %s", info.to_dict())
    except TelegramError as e:
        log.warning("get_webhook_info(after) failed: %s", e)

    # Запускаем сканер в фоне
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
    # ← вот эти ДВЕ строки тебе и нужны
    req = HTTPXRequest(connect_timeout=10, read_timeout=45, write_timeout=30)
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
        drop_pending_updates=True,
        allowed_updates=["message","callback_query","my_chat_member"],
    )

if __name__ == "__main__":
    main()
