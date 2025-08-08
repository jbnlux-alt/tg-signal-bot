import os
import time
import requests
import numpy as np
from datetime import datetime

# === Настройки ===
QUOTE = "USDT"  # торгуем к USDT
PUMP_THRESHOLD = float(os.getenv("PUMP_THRESHOLD", 0.03))  # 3% за минуту
RSI_MIN = float(os.getenv("RSI_MIN", 70))  # минимальный RSI
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", 86400))  # раз в день

BINANCE_API = "https://api.binance.com"
MEXC_API = "https://api.mexc.com"

symbols = []
last_refresh = 0


def fetch_symbols():
    """Загрузка всех USDT пар с Binance и MEXC"""
    global symbols, last_refresh
    now = time.time()
    if now - last_refresh < SYMBOL_REFRESH_SEC and symbols:
        return symbols

    pairs = set()

    # Binance
    try:
        resp = requests.get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=10).json()
        for s in resp["symbols"]:
            if s["symbol"].endswith(QUOTE):
                pairs.add(s["symbol"])
    except Exception as e:
        print("Ошибка загрузки пар Binance:", e)

    # MEXC
    try:
        resp = requests.get(f"{MEXC_API}/api/v3/exchangeInfo", timeout=10).json()
        for s in resp["symbols"]:
            if s["symbol"].endswith(QUOTE):
                pairs.add(s["symbol"])
    except Exception as e:
        print("Ошибка загрузки пар MEXC:", e)

    symbols = sorted(list(pairs))
    last_refresh = now
    print(f"[{datetime.now()}] Загружено {len(symbols)} пар")
    return symbols


def get_ohlcv_binance(symbol):
    """Берём последние 100 свечей 1m с Binance"""
    url = f"{BINANCE_API}/api/v3/klines"
    params = {"symbol": symbol, "interval": "1m", "limit": 100}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        close_prices = [float(c[4]) for c in data]
        return close_prices
    except:
        return []


def rsi(prices, period=14):
    """Расчёт RSI"""
    if len(prices) < period + 1:
        return None
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi_vals = [100 - (100 / (1 + rs))]

    for i in range(period, len(prices) - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 0
        rsi_vals.append(100 - (100 / (1 + rs)) if avg_loss != 0 else 100)

    return rsi_vals[-1]


def scanner_loop(bot, chat_id):
    """Фоновый сканер"""
    while True:
        for sym in fetch_symbols():
            prices = get_ohlcv_binance(sym)
            if not prices:
                continue

            last_price = prices[-1]
            change = (last_price - prices[-2]) / prices[-2]
            rsi_val = rsi(prices)

            if change >= PUMP_THRESHOLD and rsi_val and rsi_val >= RSI_MIN:
                pct = round(change * 100, 2)
                rsi_txt = f"{rsi_val:.2f}"
                mexc_url = f"https://www.mexc.com/exchange/{sym.replace(QUOTE,'')}_{QUOTE}"
                tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{sym}"
                msg = (
                    f"🚨 Аномальный памп: +{pct}% за 1 мин\n"
                    f"📉 Монета: ${sym}\n"
                    f"💵 Цена: {last_price}\n\n"
                    f"📊 Условия:\n"
                    f"✅ RSI: {rsi_txt} (мин {int(RSI_MIN)})\n"
                    f"✅ Порог пампа: {int(PUMP_THRESHOLD*100)}%\n"
                    f"🕒 Таймфрейм: 1m\n\n"
                    f"🎯 SHORT (MVP)\n"
                    f"💰 Риск: 0.1% | Тейк: 250%\n"
                    f"🔗 MEXC: {mexc_url}\n"
                    f"📈 TV: {tv_url}"
                )
                bot.send_message(chat_id=chat_id, text=msg)

        time.sleep(60)


