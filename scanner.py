import os, asyncio, time, math
import httpx

API_BASE = os.environ.get("MEXC_API_BASE", "https://api.mexc.com")
SCAN_SYMBOLS = [s.strip().upper() for s in os.environ.get("SCAN_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,DOGEUSDT,SHIBUSDT").split(",")]
PUMP_THRESHOLD = float(os.environ.get("PUMP_THRESHOLD", "0.70"))  # 0.70 = 70%
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "10"))        # seconds
RSI_MIN = float(os.environ.get("RSI_MIN", "70"))                  # RSI must be >= 70

# MEXC 1m klines endpoint (совместим с Binance-подобным форматом у MEXC)
# Если у тебя другой путь, просто поменяй URL-шаблон ниже.
KLINES_URL = API_BASE + "/api/v3/klines"

async def fetch_klines(symbol: str, limit: int = 100):
    params = {"symbol": symbol, "interval": "1m", "limit": str(limit)}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(KLINES_URL, params=params)
        r.raise_for_status()
        return r.json()

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = values[-i] - values[-i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def check_symbol(symbol: str):
    # Берём последние 100 свечей 1m
    data = await fetch_klines(symbol, limit=100)
    # Формат: [openTime, open, high, low, close, volume, ...]
    closes = [float(x[4]) for x in data]
    # Смотрим памп последней минуты относительно предыдущей close
    last_close = closes[-1]
    prev_close = closes[-2]
    change = (last_close - prev_close) / prev_close  # доля (0.7 = 70%)
    # RSI
    r = rsi(closes, period=14)
    return change, r, last_close

async def scanner_loop(bot, chat_id: int):
    while True:
        start = time.time()
        try:
            for sym in SCAN_SYMBOLS:
                try:
                    change, rsi_val, price = await check_symbol(sym)
                except Exception:
                    # тихо пропускаем ошибочные символы/эндпоинты
                    continue

                if change >= PUMP_THRESHOLD and (rsi_val is not None and rsi_val >= RSI_MIN):
                    pct = round(change * 100, 2)
                    rsi_txt = f"{rsi_val:.2f}" if rsi_val is not None else "n/a"

                    tv_symbol = sym  # для TradingView используем BINANCE: как визуализацию
                    msg = (
                        f"🚨 Аномальный памп: +{pct}% за 1 минуту\n"
                        f"📉 Монета: ${sym}\n"
                        f"💵 Цена: {price}\n\n"
                        f"📊 Условия:\n"
                        f"✅ RSI: {rsi_txt} (мин {int(RSI_MIN)})\n"
                        f"✅ Порог пампа: {int(PUMP_THRESHOLD*100)}%\n"
                        f"🕒 Таймфрейм: 1m\n\n"
                        f"🎯 Рекомендуем SHORT (MVP)\n"
                        f"💰 Риск: 0.1% | Тейк: 250%\n"
                    )

                    # Кнопки
                    mexc_url = f"https://www.mexc.com/exchange/{sym.replace('USDT','')}_USDT"
                    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{sym}"
                    reply_markup = {
                        "inline_keyboard": [
                            [{"text": "🔘 Открыть сделку на MEXC", "url": mexc_url}],
                            [{"text": "📈 Смотреть график (TV)", "url": tv_url}],
                        ]
                    }

                    await bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        reply_markup=reply_markup
                    )

        except Exception:
            # гасим любые падения цикла, продолжаем жить
            pass

        # Ждём до следующей итерации
        elapsed = time.time() - start
        await asyncio.sleep(max(1, SCAN_INTERVAL - int(elapsed)))
