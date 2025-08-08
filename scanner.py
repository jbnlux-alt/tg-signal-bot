# scanner.py
import asyncio
import os
import requests

PUMP_THRESHOLD = float(os.getenv("PUMP_THRESHOLD", "0.03"))
RSI_MIN = float(os.getenv("RSI_MIN", "70"))
QUOTE = os.getenv("QUOTE", "USDT")
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", "86400"))

API_URL = "https://api.binance.com/api/v3/ticker/price"  # публичный Binance API

async def scanner_loop(bot, chat_id):
    """Фоновый сканер. Отправляет тестовый сигнал и далее мониторит пары"""
    # 1. Сразу отправим тестовый сигнал при старте
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "🚨 Тестовый сигнал бота\n"
            f"📊 RSI: {RSI_MIN} (мин {int(RSI_MIN)})\n"
            f"✅ Порог пампа: {int(PUMP_THRESHOLD*100)}%\n"
            "🕒 Таймфрейм: 1m\n\n"
            "🎯 SHORT (MVP)\n"
            "💰 Риск: 0.1% | Тейк: 250%\n"
            "🔗 MEXC: https://www.mexc.com/exchange/BTC_USDT\n"
            "📈 TradingView: https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT"
        )
    )

    # 2. Дальше бесконечный цикл проверки
    while True:
        try:
            resp = requests.get(API_URL, timeout=10)
            data = resp.json()

            # Пример: возьмем BTCUSDT
            btc = next((x for x in data if x["symbol"] == "BTCUSDT"), None)
            if btc:
                price = float(btc["price"])
                print(f"[scanner] Цена BTCUSDT: {price}")

                # Отправим тестовое сообщение раз в час
                msg = (
                    f"⏱ Обновление цены BTCUSDT: {price}\n"
                    f"Порог пампа: {int(PUMP_THRESHOLD*100)}%, RSI мин: {int(RSI_MIN)}"
                )
                await bot.send_message(chat_id=chat_id, text=msg)

        except Exception as e:
            print("Ошибка в сканере:", e)

        await asyncio.sleep(3600)  # проверка раз в час (можно изменить)

if __name__ == "__main__":
    print("[INFO] Запуск сканера...")
    scanner_loop()

