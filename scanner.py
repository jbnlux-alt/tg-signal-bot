# scanner.py
import os, asyncio, aiohttp, math, time
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ==== ENV ====
PUMP_THRESHOLD     = float(os.getenv("PUMP_THRESHOLD", "0.07"))    # 7% за 1м (теперь по умолчанию 0.07)
RSI_MIN            = float(os.getenv("RSI_MIN", "70"))
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", "60"))         # сек между циклами
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", "86400"))  # раз в сутки
QUOTE              = os.getenv("QUOTE_FILTER", "USDT")
MAX_CONCURRENCY    = int(os.getenv("MAX_CONCURRENCY", "8"))

MEXC_API = "https://api.mexc.com/api/v3"

_symbols_cache: list[str] = []
_last_reload: float = 0.0
_sent_startup_ping: bool = False


async def _fetch_json(session: aiohttp.ClientSession, url: str, **params):
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_symbols() -> tuple[list[str], bool]:
    """
    Возвращает (symbols, refreshed_now)
    refreshed_now=True, если прямо сейчас обновили список.
    """
    global _symbols_cache, _last_reload
    now = time.time()
    if _symbols_cache and (now - _last_reload) < SYMBOL_REFRESH_SEC:
        return _symbols_cache, False

    async with aiohttp.ClientSession() as s:
        info = await _fetch_json(s, f"{MEXC_API}/exchangeInfo")

    syms = []
    for x in info.get("symbols", []):
        if x.get("status") == "TRADING" and x.get("quoteAsset") == QUOTE:
            # при необходимости можно отфильтровать левередж-токены (3L/3S/UP/DOWN)
            # base = x.get("baseAsset","")
            # if base.endswith(("3L","3S","4L","4S","UP","DOWN")): continue
            syms.append(x["symbol"])

    _symbols_cache = sorted(set(syms))
    _last_reload = now
    return _symbols_cache, True


async def fetch_klines_1m(session: aiohttp.ClientSession, symbol: str, limit: int = 30):
    # /klines?symbol=BTCUSDT&interval=1m&limit=30
    return await _fetch_json(session, f"{MEXC_API}/klines", symbol=symbol, interval="1m", limit=str(limit))


def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    # начальные средние по первым 'period' изменениям (Wilder)
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses += -d
    avg_gain = gains / period
    avg_loss = losses / period
    # сглаживание
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = d if d > 0 else 0.0
        loss = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


async def scanner_loop(bot, chat_id: int):
    global _sent_startup_ping
    if not _sent_startup_ping:
        await bot.send_message(chat_id=chat_id, text="🛰 Scanner online: MEXC 1m • RSI фильтр")
        _sent_startup_ping = True

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    while True:
        try:
            symbols, refreshed = await fetch_symbols()
            if refreshed:
                # уведомим раз в сутки при обновлении пула пар
                try:
                    await bot.send_message(chat_id=chat_id, text=f"🔄 Пары MEXC обновлены: {len(symbols)} (QUOTE={QUOTE})")
                except Exception:
                    pass

            if not symbols:
                await asyncio.sleep(5)
                continue

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as s:
                async def handle(sym: str):
                    async with sem:
                        try:
                            data = await fetch_klines_1m(s, sym, limit=30)
                            if not isinstance(data, list) or len(data) < 2:
                                return
                            closes = [float(x[4]) for x in data]
                            prev_c, last_c = closes[-2], closes[-1]
                            if prev_c <= 0:
                                return
                            change = (last_c - prev_c) / prev_c
                            rsi = calc_rsi(closes, period=14)
                            if rsi is None:
                                return

                            if change >= PUMP_THRESHOLD and rsi >= RSI_MIN:
                                pct = round(change * 100, 2)
                                mexc_url = f"https://www.mexc.com/exchange/{sym.replace(QUOTE,'')}_{QUOTE}"
                                # у TradingView тикеры MEXC отображаются не для всех монет; оставим ссылку на общий чарт с символом
                                tv_url   = f"https://www.tradingview.com/chart/?symbol=MEXC:{sym}"

                                text = (
                                    f"🚨 Аномальный памп: +{pct}% за 1 мин\n"
                                    f"📉 Монета: {sym}\n"
                                    f"💵 Цена: {last_c}\n\n"
                                    f"📊 Условия:\n"
                                    f"✅ RSI: {rsi:.2f} (мин {int(RSI_MIN)})\n"
                                    f"✅ Порог пампа: {int(PUMP_THRESHOLD*100)}%\n"
                                    f"🕒 Таймфрейм: 1m\n\n"
                                    f"🎯 SHORT (MVP)\n"
                                    f"💰 Риск: 0.1% | Тейк: 250%\n"
                                )

                                kb = InlineKeyboardMarkup([
                                    [InlineKeyboardButton("🔘 Открыть сделку на MEXC", url=mexc_url)],
                                    [InlineKeyboardButton("📈 Смотреть график (TradingView)", url=tv_url)],
                                ])

                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=text,
                                    reply_markup=kb,
                                    parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True
                                )
                        except Exception:
                            return

                tasks = [asyncio.create_task(handle(sym)) for sym in symbols]
                await asyncio.gather(*tasks)

        except Exception:
            pass

        await asyncio.sleep(SCAN_INTERVAL)



