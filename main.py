# main.py — универсальный запуск: polling (c health-сервером) или webhook.
# Для быстрой починки ставь USE_POLLING=true (рекомендовано на free-плане).

import os, logging, asyncio
from contextlib import suppress
from aiohttp import web
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
from telegram.error import TelegramError
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("entry")

def _clean(s: str | None) -> str:
    if not s: return ""
    return "".join(ch for ch in s.replace("\ufeff","").replace("\u200b","").replace("\u2060","") if ch.isprintable()).strip()

TOKEN = _clean(os.getenv("TOKEN"))
CHAT_ID = int(_clean(os.getenv("CHAT_ID","0")) or "0")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_BASE = _clean(os.getenv("WEBHOOK_BASE"))
WEBHOOK_SECRET = _clean(os.getenv("WEBHOOK_SECRET",""))
USE_POLLING = os.getenv("USE_POLLING","false").lower() == "true"

if not TOKEN:   raise RuntimeError("TOKEN is required")
if not CHAT_ID: raise RuntimeError("CHAT_ID is required")

# ---------- handlers ----------
async def cmd_start(update, ctx): await update.message.reply_text("✅ bot is alive")
async def cmd_ping(update, ctx):  await update.message.reply_text("pong")
async def any_text(update, ctx):  await update.message.reply_text("✅ got it")

def build_app():
    req = HTTPXRequest(connect_timeout=10, read_timeout=45, write_timeout=30)
    app = Application.builder().token(TOKEN).request(req).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping",  cmd_ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_text))
    app.add_handler(MessageHandler(filters.ALL, lambda *_: None))
    return app

# ---------- health server on / and /healthz ----------
async def start_health_server():
    async def ok(_): return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/healthz", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("health server on :%d", PORT)
    return runner

async def run_polling_mode():
    log.info("Starting in POLLING mode (health server on :%d)", PORT)
    runner = await start_health_server()

    tg = build_app()
    await tg.initialize()
    await tg.start()

    # запускаем сканер
    scanner_task = tg.create_task(scanner_loop(tg.bot, CHAT_ID))
    # запускаем polling
    await tg.updater.start_polling(drop_pending_updates=True, allowed_updates=["message","callback_query","my_chat_member"])
    try:
        await tg.updater.wait_until_closed()
    finally:
        scanner_task.cancel()
        with suppress(asyncio.CancelledError):
            await scanner_task
        await tg.stop()
        await tg.shutdown()
        # гасим health
        await runner.cleanup()

async def run_webhook_mode():
    if not WEBHOOK_BASE:
        raise RuntimeError("WEBHOOK_BASE is required in webhook mode")
    path = "/webhook" if not WEBHOOK_SECRET else f"/webhook/{WEBHOOK_SECRET}"
    url  = WEBHOOK_BASE.rstrip("/") + path
    log.info("Starting in WEBHOOK mode :%d url=%s", PORT, url)

    tg = build_app()

    async def post_init(app: Application):
        try:
            info = await app.bot.get_webhook_info()
            log.info("Webhook BEFORE set: %s", info.to_dict())
            await app.bot.set_webhook(
                url=url,
                allowed_updates=["message","callback_query","my_chat_member"],
                drop_pending_updates=True,
            )
            info = await app.bot.get_webhook_info()
            log.info("Webhook AFTER set: %s", info.to_dict())
        except TelegramError as e:
            log.error("set_webhook failed: %s", e)

        task = app.create_task(scanner_loop(app.bot, CHAT_ID))
        app.bot_data["scanner_task"] = task
        log.info("scanner_task started")

    async def post_shutdown(app: Application):
        task = app.bot_data.get("scanner_task")
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            log.info("scanner_task cancelled")

    tg.post_init = post_init
    tg.post_shutdown = post_shutdown

    tg.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=path,
        webhook_url=url,
        drop_pending_updates=True,
        allowed_updates=["message","callback_query","my_chat_member"],
    )

def main():
    if USE_POLLING:
        asyncio.run(run_polling_mode())
    else:
        run_webhook_mode()

if __name__ == "__main__":
    main()
