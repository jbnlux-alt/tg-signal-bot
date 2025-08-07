import os, asyncio, time, re
import httpx

API_BASE = os.environ.get("MEXC_API_BASE", "https://api.mexc.com")
PUMP_THRESHOLD = float(os.environ.get("PUMP_THRESHOLD", "0.70"))  # 0.70 = 70%
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "10"))        # сек между циклами
RSI_MIN = float(os.environ.get("RSI_MIN", "70"))
QUOTE = os.environ.get("QUOTE_FILTER", "USDT")                    # котировка
SYMBOL_REFRESH_SEC = int(os.environ.get("SYMBOL_REFRESH_SEC", "600"))  # раз в 10 мин
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "8"))     # одновременно запросов
INCLUDE_LEVERAGED = os.environ.get("INCLUDE_LEVERAGED", "false").lower() == "true"

KLINES_URL = f"{API_BASE}/api/v3/klines"
EXCHANGE_INFO_URL = f"{API_BASE}/api/v3/exchangeInfo"

_leveraged_pat = re.compile(r"(?:[234]L|[234]S|UP|DOWN)$", re.IGNORECASE)

async def fetch_exchange_info():
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(EXCHANGE_INFO_URL)
        r.raise_for_status()
        return r.json()

def want_symbol(sym_obj):
    # sym_obj формат MEXC: { "symbol": "BTCUSDT", "status":"TRADING", "baseAsset":"BTC", "quoteAsset":"USDT", ... }
    if sym_obj.get("status") != "TRADING":
        return False
    if sym_obj.get("quoteAsset") != QUOTE:
        return False
    sym = sym_obj.get("symbol","")
    if not INCLUDE_LEVERAGED and _leveraged_pat.search(sym.replace(QUOTE,"")):
        return False
    return True

async def get_all_symbols():
    info = await fetch_exchange_info()
    # MEXC иногда возвращает "symbols": [...]
    symbols = []
    for s in info.get("symbols", []):
        if want_symbol(s):
            symbols.append(s["symbol"])
    return sorted(set(symbols))

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = values[-i] - values[-i-1]
        if delta >= 0:
            gains += delta
        else:
            losses += -delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def fetch_klines(client, symbol: str, limit: int = 100):
    params = {"symbol": symbol, "interval": "1m", "limit": str(limit)}
    r = await client.get(KLINES_URL, params=params)
    r.raise_for_status()
    return r.json()

async def check_symbol(client, symbol: str):
    # Для экономии трафика хватит 30 свечей (RSI14 + 1m изменение)
    data = await fetch_klines(client, symbol, limit=30)
    closes = [float(x[4]) for x in data]
    last_close, prev_close = closes[-1], closes[-2]
    change = (last_close - prev_close) / prev_close
    r = rsi(closes, period=14)
    return symbol, change, r, last_close

async def scanner_loop(bot, chat_id: int):
    symbols = []
    last_reload = 0.0
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _task(sym, client):
        async with sem:
            try:
                return await check_symbol(client, sym)
            except Exception:
                return None

    while True:
        now = time.time()
        try:
            # периодически обновляем список символов
            if not symbols or (now - last_reload) >= SYMBOL_REFRESH_SEC:
                try:
                    symbols = await get_all_symbols()
                    last_reload = now
                except Exception:
                    # если не удалось — оставляем предыдущий список
                    pass

            if not symbols:
                await asyncio.sleep(5)
                continue

            async with httpx.AsyncClient(timeout=10) as client:
                tasks = [_task(sym, client) for sym in symbols]
                for coro in asyncio.as_completed(tasks):
                    res = await coro
                    if not res:
                        continue
                    sym, change, rsi_val, price = res
                    if change >= PUMP_THRESHOLD and (rsi_val is not None and rsi_val >= RSI_MIN):
                        pct = round(change * 100, 2)
                        rsi_txt = f"{rsi_val:.2f}"
                        mexc_url = f"https://www.mexc.com/exchange/{sym.replace(QUOTE,'')}_{QUOTE}"
                        tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{sym}"
                        msg = (
                            f"🚨 Аномальный памп: +{pct}% за 1 мин\n"
                            f"📉 Монета: ${sym}\n"
                            f"💵 Цена: {price}\n\n"
                            f"📊 Условия:\n"
                            f"✅ RSI: {rsi_txt} (мин {int(RSI_MIN)})\n"
                            f"✅ Порог пампа: {int(PUMP_THRESHOLD*100)}%\n"
                            f"🕒 Таймфрейм: 1m\n\n"
                            f"🎯 SHORT (MVP)\n"
                            f"💰 Риск: 0.1% | Тейк: 250%\n"
                        )
                        reply_markup = {
                            "inline_keyboard": [
                                [{"text": "🔘 Открыть сделку на MEXC", "url": mexc_url}],
                                [{"text": "📈 График (TradingView)", "url": tv_url}],
                            ]
                        }
                        await bot.send_message(chat_id=chat_id, text=msg, reply_markup=reply_markup)

        except Exception:
            # не падаем, продолжаем цикл
            pass

        await asyncio.sleep(SCAN_INTERVAL)
