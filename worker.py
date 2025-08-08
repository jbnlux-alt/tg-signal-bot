import os, asyncio, logging, signal
from telegram import Bot
from scanner import scanner_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

async def main():
    token = os.getenv("TOKEN")
    chat_id = int(os.getenv("CHAT_ID", "0"))
    if not token:
        raise RuntimeError("TOKEN is required")
    if not chat_id:
        raise RuntimeError("CHAT_ID is required")

    bot = Bot(token=token)
    task = asyncio.create_task(scanner_loop(bot, chat_id))
    logging.info("scanner_loop started in worker")

    # ждём SIGTERM/SIGINT для graceful shutdown
    stop = asyncio.Future()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set_result, None)
        except NotImplementedError:
            # Windows / ограниченные окружения
            pass

    try:
        await stop
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logging.info("scanner_loop cancelled cleanly")

if __name__ == "__main__":
    asyncio.run(main())
