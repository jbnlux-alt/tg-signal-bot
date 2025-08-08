import os, asyncio, logging, signal
from telegram import Bot
from telegram.request import HTTPXRequest
from telegram.error import NetworkError
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("worker")

async def main():
    token = os.getenv("TOKEN"); chat_id = int(os.getenv("CHAT_ID", "0"))
    if not token: raise RuntimeError("TOKEN is required")
    if not chat_id: raise RuntimeError("CHAT_ID is required")

    req = HTTPXRequest(connect_timeout=10, read_timeout=30)
    bot = Bot(token=token, request=req)

    try:
        me = await bot.get_me()
        log.info("Bot online: @%s", me.username)
    except NetworkError as e:
        log.warning("Telegram get_me failed at startup (ignored): %s", e)

    task = asyncio.create_task(scanner_loop(bot, chat_id))
    log.info("scanner_loop started in worker")

    stop = asyncio.Future()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: loop.add_signal_handler(sig, stop.set_result, None)
        except NotImplementedError: pass

    try:
        await stop
    finally:
        task.cancel()
        try:    await task
        except asyncio.CancelledError:
            log.info("scanner_loop cancelled cleanly")

if __name__ == "__main__":
    asyncio.run(main())
