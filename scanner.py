import os
import time
import requests
import math
from datetime import datetime

# Настройки из переменных окружения
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
QUOTE = "USDT"
PUMP_THRESHOLD = float(os.getenv("PUMP_THRESHOLD", 0.03))  # 3% по умолчанию
RSI_MIN = float(os.getenv("RSI_MIN", 70))
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", 86400))  # раз в день

# Кэш пар
symbols_cache = []
last_symbols_update = 0


def get_all_symbols():
    """Получить все USDT пары с MEXC"""
    global symbols_cache, last_symbols_update
    now = time.time()
    if now - last_symbols_update < SYMBOL_REFRESH_SEC and symbols_cache:
        return symbols_cache

    url = "https://api.mexc.com/api/v3/exchangeInfo"
    data = requests.get(url, timeout=10).json()
    symbols_cache = [
        s["symbol"] for s in data["symbols"]
        if s["quoteAsset"] == QUOTE and s["status"] == "TRADING"
    ]
    last_symbols_update = now
    print(f"[{datetime.now()}] Обновлено {len(symbols_cache)} пар с MEXC")
    return symbols_cache


def get_klines(symbol):
    """Получить 2 последних свечи 1m для расчета изменения и RSI"""
    url = f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval=1m&limit=15"
    data = requests.get(url, timeout=10).json()
    return data


def calc_rsi(closes, period=14):
    """Расчет RSI"""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 1
    rs = avg_gain / avg_loss if avg_loss != 0 else math.inf
    return 100 - (100 / (1 + rs))


def send_signal(sym, price, change, rsi_val):
    pct = round(change * 100, 2)
    rsi_txt = f"{rsi_val:.2f}" if rsi_val else "N/A"
    mexc_url = f"https://www.mexc.com/exchange/{sym.replace(QUOTE,'')}_{QUOTE}"
    tv_url = f"https://www.tradingview.com/chart/?symbol=MEXC:{sym}"

    msg = (
        f"🚨 Аномальный памп: +{pct}% за 1 мин\n"
        f"📉 Монета: ${sym}\n"
        f"💵 Цена: {price}\n\n"
        f"📊 Условия:\n"
        f"✅ RSI: {rsi_txt} (мин {int(RSI_MIN)})\n"
        f"✅ Порог пампа: {int(PUMP_THRESHOLD*100)}%\n"
        f"🕒 Таймфрейм: 1m\n\n"
        f"🎯 SHORT (MVP)\n"
        f"💰 Риск: 0.1% | Тейк: 250%\n\n"
        f"🔗 MEXC: {mexc_url}\n"
        f"📈 TradingView: {tv_url}"
    )

    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": True}
    )


def scanner_loop():
    while True:
        symbols = get_all_symbols()
        for sym in symbols:
            try:
                klines = get_klines(sym)
                close_prev = float(klines[-2][4])
                close_last = float(klines[-1][4])
                change = (close_last - close_prev) / close_prev
                closes = [float(c[4]) for c in klines]
                rsi_val = calc_rsi(closes)

                if change >= PUMP_THRESHOLD and rsi_val is not None and rsi_val >= RSI_MIN:
                    send_signal(sym, close_last, change, rsi_val)

            except Exception as e:
                print(f"Ошибка по {sym}: {e}")

        time.sleep(60)  # проверка каждую минуту


if __name__ == "__main__":
    scanner_loop()


if __name__ == "__main__":
    print("[INFO] Запуск сканера...")
    scanner_loop()

