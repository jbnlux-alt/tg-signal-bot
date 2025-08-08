# scanner.py
import os, asyncio, aiohttp, math, time
from datetime import datetime

PUMP_THRESHOLD = float(os.getenv("PUMP_THRESHOLD", "0.70"))  # 0.70 = 70%
RSI_MIN = float(os.getenv("RSI_MIN", "70"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", "86400"))
QUOTE = os.getenv("QUOTE_FILTER", "USDT")

MEXC = "https://api.mexc.com/api/v3"
symbols_cache, last_reload = [], 0.0

async def fetch_symbols():
    global symbols_cache, last_reload
    now = time.time()
    if symbols_cache and (now - last_reload) < SYMBOL_REFRESH_SEC:
        return symbols_cache
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        r = await s.get(f"{MEXC}/exchangeInfo"); info = await r.json()
    symbols_cache = sorted({x["symbol"] for x in info.get("symbols", [])
                            if x.get("status")=="TRADING" and x.get("quoteAsset")==QUOTE})
    last_reload = now
    return symbols_cache

async def fetch_klines(session, symbol, limit):
    params = {"symbol": symbol, "interval":"1m", "limit": str(limit)}
    r = await session.get(f"{MEXC}/klines", params=params)
    r.raise_for_status()
    return await r.json()

def calc_rsi(closes, period=14):
    if len(closes) < period+1: return None
    gains=losses=0.0
    for i in range(1, period+1):
        d = closes[-i]-closes[-i-1]
        gains += d if d>0 else 0
        losses += -d if d<0 else 0
    if losses==0: return 100.0
    rs = gains/period / (losses/period)
    return 100 - 100/(1+rs)

async def scanner_loop(bot, chat_id):
    """Асинхронный сканер, уважающий SCAN_INTERVAL"""
    await bot.send_message(chat_id=chat_id, text="🛰 Scanner online (MEXC 1m, RSI)")
    while True:
        try:
            syms = await fetch_symbols()
            if not syms:
                await asyncio.sleep(5); continue
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                for sym in syms:
                    try:
                        data = await fetch_klines(s, sym, limit=30)
                        closes = [float(x[4]) for x in data]
                        if len(closes) < 2: continue
                        change = (closes[-1]-closes[-2])/closes[-2]
                        rsi = calc_rsi(closes, period=14)
                        if rsi is None: continue
                        if change >= PUMP_THRESHOLD and rsi >= RSI_MIN:
                            pct = round(change*100,2)
                            price = closes[-1]
                            mexc_url = f"https://www.mexc.com/exchange/{sym.replace(QUOTE,'')}_{QUOTE}"
                            tv_url = f"https://www.tradingview.com/chart/?symbol=MEXC:{sym}"
                            msg = (
                                f"🚨 Аномальный памп: +{pct}% за 1м\n"
                                f"📉 Монета: {sym}\n"
                                f"💵 Цена: {price}\n\n"
                                f"📊 Условия:\n"
                                f"✅ RSI: {rsi:.2f} (мин {int(RSI_MIN)})\n"
                                f"✅ Порог пампа: {int(PUMP_THRESHOLD*100)}%\n"
                                f"🕒 Таймфрейм: 1m\n\n"
                                f"🔗 MEXC: {mexc_url}\n"
                                f"📈 TV: {tv_url}"
                            )
                            await bot.send_message(chat_id=chat_id, text=msg, disable_web_page_preview=True)
                    except Exception:
                        continue
        except Exception:
            pass
        await asyncio.sleep(SCAN_INTERVAL)


