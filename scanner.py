# scanner.py
import os
import time
import math
import asyncio
import logging
import aiohttp
from typing import List, Tuple, Optional

from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut, RetryAfter, BadRequest

log = logging.getLogger("scanner")

# ===================== ENV =====================
PUMP_THRESHOLD     = float(os.getenv("PUMP_THRESHOLD", "0.07"))      # 7% за 1м
RSI_MIN            = float(os.getenv("RSI_MIN", "70"))               # порог RSI
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", "60"))           # сек между итерациями
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", "86400"))   # раз в сутки обновляем список
QUOTE              = os.getenv("QUOTE_FILTER", "USDT")               # котировка
MAX_CONCURRENCY    = int(os.getenv("MAX_CONCURRENCY", "8"))
COOLDOWN_SEC       = int(os.getenv("COOLDOWN_SEC", "900"))           # антиспам по символу
STARTUP_PING       = os.getenv("STARTUP_PING", "true").lower() == "true"

MIN_COIN_AGE_DAYS  = int(os.getenv("MIN_COIN_AGE_DAYS", "30"))       # не младше
BTC_FILTER         = os.getenv("BTC_FILTER", "off").lower()          # 'on'/'off'

DISABLE_CHARTS     = os.getenv("DISABLE_CHARTS", "false").lower() == "true"

MEXC_SPOT_API      = "https://api.mexc.com/api/v3"

# таймауты HTTP
HTTP_TOTAL_TIMEOUT = int(os.getenv("HTTP_TOTAL_TIMEOUT", "15"))

# ретраи телеги
TG_MAX_ATTEMPTS    = int(os.getenv("TG_MAX_ATTEMPTS", "5"))
TG_BACKOFF_BASE    = float(os.getenv("TG_BACKOFF_BASE", "1.5"))

# ===================== Charts (optional) =====================
HAVE_CHARTS = False
try:
    if not DISABLE_CHARTS:
        # charts.py должен лежать рядом с проектом
        from charts import render_chart_image, klines_to_df, compute_sr_levels
        HAVE_CHARTS = True
except Exception as e:
    log.warning("charts disabled: %s", e)
    HAVE_CHARTS = False

# ===================== In-memory state =====================
_symbols_cache: List[str] = []
_last_reload: float = 0.0
_symbols_backoff_until: float = 0.0  # когда можно снова дергать /exchangeInfo


_last_sent: dict[str, float] = {}            # антиспам по символу
_last_sent_lock = asyncio.Lock()

_sent_startup_ping = False

# ===================== Telegram helpers =====================
async def tg_call(bot, method: str, *args, **kwargs):
    """
    Надёжный вызов Telegram API с ретраями и экспоненциальным бэкоффом.
    Возвращает результат или None.
    """
    for attempt in range(1, TG_MAX_ATTEMPTS + 1):
        try:
            return await getattr(bot, method)(*args, **kwargs)

        except RetryAfter as e:
            delay = float(getattr(e, "retry_after", 1.0)) + 0.5
            await asyncio.sleep(delay)

        except (NetworkError, TimedOut) as e:
            if attempt == TG_MAX_ATTEMPTS:
                log.warning("TG %s failed after %d tries: %s", method, attempt, e)
                return None
            await asyncio.sleep(TG_BACKOFF_BASE ** attempt)

        except BadRequest as e:
            log.warning("TG BadRequest in %s: %s", method, e)
            return None

        except Exception as e:
            log.warning("TG error in %s: %r", method, e)
            return None

async def tg_send_message(bot, **kwargs):
    return await tg_call(bot, "send_message", **kwargs)

async def tg_send_photo(bot, **kwargs):
    return await tg_call(bot, "send_photo", **kwargs)

# ===================== HTTP helpers =====================
async def _fetch_json(session: aiohttp.ClientSession, url: str, **params):
    timeout = aiohttp.ClientTimeout(total=HTTP_TOTAL_TIMEOUT)
    async with session.get(url, params=params, timeout=timeout) as r:
        r.raise_for_status()
        return await r.json()

# ===================== Data fetchers =====================
async def fetch_symbols() -> tuple[list[str], bool]:
    """
    Возвращает (symbols, refreshed_now).
    refreshed_now=True — только когда реально обновили кэш.
    Если API вернуло 0 — считаем сбоем, кэш не трогаем, уходим в бэкофф.
    """
    global _symbols_cache, _last_reload, _symbols_backoff_until

    now = time.time()

    # уважаем бэкофф после неудачи, если кэш уже есть
    if now < _symbols_backoff_until and _symbols_cache:
        return _symbols_cache, False

    # не чаще чем раз в SYMBOL_REFRESH_SEC
    if _symbols_cache and (now - _last_reload) < SYMBOL_REFRESH_SEC:
        return _symbols_cache, False

    try:
        async with aiohttp.ClientSession() as s:
            info = await _fetch_json(s, f"{MEXC_SPOT_API}/exchangeInfo")

        syms: list[str] = []
        for x in info.get("symbols", []):
            if x.get("status") == "TRADING" and x.get("quoteAsset") == QUOTE:
                syms.append(x["symbol"])

        # если пусто — не обновляем кэш, ставим бэкофф
        if not syms:
            _symbols_backoff_until = now + 300  # 5 минут
            logging.getLogger("scanner").warning(
                "fetch_symbols: API вернуло 0 символов; keep cache=%d, backoff 5m.",
                len(_symbols_cache),
            )
            return _symbols_cache, False

        _symbols_cache = sorted(set(syms))
        _last_reload = now
        _symbols_backoff_until = 0.0
        return _symbols_cache, True

    except Exception as e:
        # сеть/429 и т.п. — не трогаем кэш и уходим в бэкофф
        _symbols_backoff_until = now + 300
        logging.getLogger("scanner").warning(
            "fetch_symbols failed: %s; keep cache=%d, backoff 5m.",
            e, len(_symbols_cache),
        )
        return _symbols_cache, False


async def fetch_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int):
    # формат: /klines?symbol=BTCUSDT&interval=1m&limit=30
    return await _fetch_json(session, f"{MEXC_SPOT_API}/klines",
                             symbol=symbol, interval=interval, limit=str(limit))

# ===================== Indicators =====================
def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses += -d
    avg_gain = gains / period
    avg_loss = losses / period
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

# ===================== Filters =====================
async def btc_ok(session: aiohttp.ClientSession) -> bool:
    """Если включён BTC_FILTER=on, не шортим, когда BTC в явном трендовом росте."""
    if BTC_FILTER != "on":
        return True
    try:
        d = await fetch_klines(session, "BTCUSDT", "15m", 40)
        closes = [float(x[4]) for x in d]
        if len(closes) < 20:
            return True
        # простая эвристика: последняя цена выше SMA(20) + 1*std — считаем "бычий импульс"
        sma = sum(closes[-20:]) / 20.0
        std = (sum((c - sma) ** 2 for c in closes[-20:]) / 20.0) ** 0.5
        return not (closes[-1] > sma + std)
    except Exception as e:
        log.warning("btc_ok failed (ignore): %s", e)
        return True

async def coin_age_ok(session: aiohttp.ClientSession, symbol: str) -> bool:
    """Монете не меньше MIN_COIN_AGE_DAYS (по 1d свечам на споте)."""
    if MIN_COIN_AGE_DAYS <= 0:
        return True
    try:
        d = await fetch_klines(session, symbol, "1d", min(1000, MIN_COIN_AGE_DAYS + 5))
        return len(d) >= MIN_COIN_AGE_DAYS
    except Exception as e:
        log.warning("coin_age_ok %s failed (ignore): %s", symbol, e)
        return True

# ===================== Core loop =====================
async def scanner_loop(bot, chat_id: int):
    global _sent_startup_ping

    if STARTUP_PING and not _sent_startup_ping:
        try:
            await tg_send_message(
                bot,
                chat_id=chat_id,
                text="🛰 Scanner online: MEXC 1m • RSI фильтр"
            )
        except Exception:
            pass
        _sent_startup_ping = True

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    while True:
        try:
            symbols, refreshed = await fetch_symbols()
            if refreshed:
                await tg_send_message(
                    bot, chat_id=chat_id,
                    text=f"🔄 Пары MEXC обновлены: {len(symbols)} (QUOTE={QUOTE})"
                )

            if not symbols:
                await asyncio.sleep(10)
                continue

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TOTAL_TIMEOUT)) as s:
                # BTC фильтр
                if not await btc_ok(s):
                    await asyncio.sleep(SCAN_INTERVAL)
                    continue

                async def handle(sym: str):
                    async with sem:
                        try:
                            # возраст монеты
                            if not await coin_age_ok(s, sym):
                                return

                            data = await fetch_klines(s, sym, "1m", 40)
                            if not isinstance(data, list) or len(data) < 20:
                                return

                            closes = [float(x[4]) for x in data]
                            prev_c, last_c = closes[-2], closes[-1]
                            if prev_c <= 0:
                                return

                            change = (last_c - prev_c) / prev_c
                            rsi = calc_rsi(closes, 14)
                            if rsi is None:
                                return

                            if change >= PUMP_THRESHOLD and rsi >= RSI_MIN:
                                # --- антиспам по символу ---
                                now = time.time()
                                async with _last_sent_lock:
                                    last = _last_sent.get(sym, 0.0)
                                    if now - last < COOLDOWN_SEC:
                                        return
                                    _last_sent[sym] = now
                                # ----------------------------

                                pct = round(change * 100, 2)

                                mexc_url = f"https://www.mexc.com/exchange/{sym.replace(QUOTE,'')}_{QUOTE}"
                                tv_url   = f"https://www.tradingview.com/chart/?symbol=MEXC:{sym}"

                                # Текст сигнала
                                lines = [
                                    f"🚨 Аномальный памп: +{pct}% за 1 мин",
                                    f"📉 Монета: {sym}",
                                    f"💵 Цена: {last_c}",
                                    "",
                                    "📊 Условия:",
                                    f"✅ RSI: {rsi:.2f} (мин {int(RSI_MIN)})",
                                    f"✅ Порог пампа: {int(PUMP_THRESHOLD * 100)}%",
                                    "🕒 Таймфрейм: 1m",
                                    "",
                                    "🎯 SHORT (MVP)",
                                    "💰 Риск: 0.1% | Тейк: 250%"
                                ]
                                text = "\n".join(lines)

                                kb = InlineKeyboardMarkup([
                                    [InlineKeyboardButton("🔘 Открыть сделку на MEXC", url=mexc_url)],
                                    [InlineKeyboardButton("📈 TradingView", url=tv_url)],
                                ])

                                # Рендер графика (если доступен charts.py)
                                img = None
                                if HAVE_CHARTS:
                                    try:
                                        df = klines_to_df(data, symbol=sym, interval="1m")
                                        sr = compute_sr_levels(df)
                                        img = render_chart_image(symbol=sym, df=df, sr_levels=sr, title=f"{sym} • 1m • S/R levels")
                                    except Exception as e:
                                        log.warning("chart render failed for %s: %s", sym, e)
                                        img = None

                                if img is not None:
                                    await tg_send_photo(
                                        bot,
                                        chat_id=chat_id,
                                        photo=img,
                                        caption=text,
                                        parse_mode=ParseMode.HTML,
                                        reply_markup=kb,
                                    )
                                else:
                                    await tg_send_message(
                                        bot,
                                        chat_id=chat_id,
                                        text=text,
                                        parse_mode=ParseMode.HTML,
                                        reply_markup=kb,
                                        disable_web_page_preview=True,
                                    )

                        except Exception as e:
                            # локальную ошибку символа не даём уронить проход
                            log.debug("worker error %s: %s", sym, e)

                tasks = [asyncio.create_task(handle(sym)) for sym in symbols]
                await asyncio.gather(*tasks)

        except Exception as e:
            log.error("scanner_loop tick failed: %s", e)

        await asyncio.sleep(SCAN_INTERVAL)
