import os
import time
import requests
from datetime import datetime

# --- Константы и настройки ---
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
QUOTE = "USDT"  # торгуемая пара к USDT
PUMP_THRESHOLD = 0.01  # 1% за минуту
RSI_MIN = 70  # минимальный RSI для сигнала
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", 86400))  # обновление списка пар раз в день

# --- Глобальный список символов ---
symbols = []
last_refresh = 0

# --- Функция отправки в Telegram ---
def send_signal(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[ERROR] Не удалось отправить сообщение: {e}")

# --- Получаем список торговых пар ---
def fetch_symbols():
    url = "https://api.binance.com/api/v3/exchangeInfo"
    try:
        resp = requests.get(url, timeout=10).json()
        pairs = [s["symbol"] for s in resp["symbols"] if s["symbol"].endswith(QUOTE)]
        print(f"[INFO] Загружено {len(pairs)} пар")
        return pairs
    except Exception as e:
        print(f"[ERROR] Ошибка загрузки списка пар: {e}")
        return []

# --- Получаем последние свечи и считаем RSI ---
def get_rsi(symbol, interval="1m", period=14):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={period+1}"
        klines = requests.get(url, timeout=10).json()
        closes = [float(k[4]) for k in klines]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff >= 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    except Exception as e:
        print(f"[ERROR] RSI {symbol}: {e}")
        return None

# --- Проверяем сигналы ---
def check_signal(sym):
    try:
        # Цена сейчас и минуту назад
        url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1m&limit=2"
        klines = requests.get(url, timeout=10).json()
        price_old = float(klines[0][4])
        price_new = float(klines[1][4])
        change = (price_new - price_old) / price_old

        rsi_val = get_rsi(sym)

        if change >= PUMP_THRESHOLD and (rsi_val is not None and rsi_val >= RSI_MIN):
            pct = round(change * 100, 2)
            rsi_txt = f"{rsi_val:.2f}"
            mexc_url = f"https://www.mexc.com/exchange/{sym.replace(QUOTE,'')}_{QUOTE}"
            tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{sym}"
            msg = (
                f"🚨 Аномальный памп: +{pct}% за 1 мин\n"
                f"📉 Монета: ${sym}\n"
                f"💵 Цена: {price_new}\n\n"
                f"📊 Условия:\n"
                f"✅ RSI: {rsi_txt} (мин {int(RSI_MIN)})\n"
                f"✅ Порог пампа: {int(PUMP_THRESHOLD*100)}%\n"
                f"🕒 Таймфрейм: 1m\n\n"
                f"🎯 SHORT (MVP)\n"
                f"💰 Риск: 0.1% | Тейк: 250%\n\n"
                f"📈 <a href='{tv_url}'>TradingView</a> | <a href='{mexc_url}'>MEXC</a>"
            )
            send_signal(msg)
    except Exception as e:
        print(f"[ERROR] Сигнал {sym}: {e}")

# --- Основной цикл ---
def scanner_loop():
    global symbols, last_refresh
    while True:
        now = time.time()
        # Обновление списка пар
        if now - last_refresh > SYMBOL_REFRESH_SEC or not symbols:
            symbols = fetch_symbols()
            last_refresh = now

        for sym in symbols:
            check_signal(sym)

        time.sleep(2)  # задержка между запросами к разным монетам

if __name__ == "__main__":
    print("[INFO] Запуск сканера...")
    scanner_loop()

