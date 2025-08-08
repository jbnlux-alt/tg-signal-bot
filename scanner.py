# scanner.py
import os, asyncio, aiohttp, time, logging
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ---------- Логи ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("scanner")

# ---------- Анти-спам (кулдаун) ----------
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "900"))  # 15 минут по умолчанию
_last_sent: dict[str, float] = {}
_last_sent_lock = asyncio.Lock()

# ---------- ENV (Render → Environment) ----------
PUMP_THRESHOLD     = float(os.getenv("PUMP_THRESHOLD", "0.07"))     # 7% за 1м
RSI_MIN            = float(os.getenv("RSI_MIN", "70"))              # порог RSI
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", "60"))          # сек между итерациями
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", "86400"))  # обновление списка пар (сек)
QUOTE              = os.getenv("QUOTE_FILTER", "USDT")              # котировка
MAX_CONCURRENCY    = int(os.getenv("MAX_CONCURRENCY", "8"))         # параллельные запросы

MEXC_API = "https://api.mexc.com/api/v3"
OPEN_API = "https://www.mexc.com/open/api/v2"

# ---------- Кэш списка пар и бэк-офф ----------
_symbols_cache: list[str] = []
_last_reload: float = 0.0
_next_pairs_fetch_at: float = 0.0   # не дёргать API раньше этого времени
_sent_startup_ping: bool = False

_HTTP_HEADERS = {
    "User-Agent": "TradeSignalFilterBot/1.0 (+render)",
    "Accept": "application/json",
}

# ---------- HTTP helper ----------
async def _fetch_json(session: aiohttp.ClientSession, url: str, **params):
    async with session.get(url, params=params, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        return await r.json(content_type=None)  # иногда content-type ломают

# ---------- Загрузка всех USDT-пар MEXC (с бэк-оффом и фолбэками) ----------
async def fetch_symbols() -> tuple[list[str], bool]:
    """
    Возвращает (symbols, refreshed_now).
    Бэк-офф работает даже при пустом кэше. Фолбэки: /exchangeInfo → /ticker/price → /open/api/v2/market/symbols.
    refreshed_now=True — только если кэш реально обновился валидными данными.
    """
    global _symbols_cache, _last_reload, _next_pairs_fetch_at
    now = time.time()

    # соблюдаем окно, даже если кэш пуст
    if now < _next_pairs_fetch_at:
        return _symbols_cache, False

    # если недавно обновляли — не трогаем
    if (now - _last_reload) < SYMBOL_REFRESH_SEC and _symbols_cache:
        return _symbols_cache, False

    symbols: list[str] = []
    try:
        async with aiohttp.ClientSession(headers=_HTTP_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as s:
            # 1) Основной: /api/v3/exchangeInfo
            try:
                info = await _fetch_json(s, f"{MEXC_API}/exchangeInfo")
                raw = info.get("symbols") or []
                if raw:
                    symbols = [
                        x["symbol"] for x in raw
                        if x.get("status") == "TRADING" and x.get("quoteAsset") == QUOTE
                    ]
            except Exception as e:
                log.warning("exchangeInfo failed: %s", e)

            # 2) Фолбэк: /api/v3/ticker/price → фильтр по суффиксу QUOTE
            if not symbols:
                try:
                    prices = await _fetch_json(s, f"{MEXC_API}/ticker/price")
                    cand = [it["symbol"] for it in prices if isinstance(it, dict) and it.get("symbol", "").endswith(QUOTE)]
                    # фильтр левередж-токенов по суффиксам
                    bad_suffixes = ("3L", "3S", "4L", "4S", "5L", "5S", "UP", "DOWN")
                    def ok(sym: str) -> bool:
                        base = sym[: -len(QUOTE)] if sym.endswith(QUOTE) else sym
                        return not any(base.endswith(suf) for suf in bad_suffixes)
                    symbols = [sym for sym in cand if ok(sym)]
                except Exception as e:
                    log.warning("ticker/price fallback failed: %s", e)

            # 3) Фолбэк: /open/api/v2/market/symbols (возвращает BTC_USDT → конвертируем в BTCUSDT)
            if not symbols:
                try:
                    j = await _fetch_json(s, f"{OPEN_API}/market/symbols")
                    data = j.get("data") or []
                    conv = []
                    for it in data:
                        state = (it.get("state") or "").upper()
                        if state in ("ENABLED", "ENALBED", "ONLINE"):  # видели опечатки в ответах
                            sym = it.get("symbol", "")
                            if "_" in sym:
                                base, quote = sym.split("_", 1)
                                if quote == QUOTE:
                                    conv.append(f"{base}{quote}")
                    symbols = conv
                except Exception as e:
                    log.warning("open/api/v2 fallback failed: %s", e)
    except Exception as e:
        log.warning("pairs session failed: %s", e)

    symbols = sorted(set(symbols))

    if symbols:
        _symbols_cache = symbols
        _last_reload = now
        _next_pairs_fetch_at = now + SYMBOL_REFRESH_SEC
        log.info("Pairs updated: %d (QUOTE=%s)", len(_symbols_cache), QUOTE)
        return _symbols_cache, True
    else:
        # пусто — кэш не трогаем, ждём 5 минут
        _next_pairs_fetch_at = now + 300
        log.warning("MEXC returned 0 symbols (all fallbacks). Keep cache=%d. Backoff 5m.", len(_symbols_cache))
        return _symbols_cache, False

# ---------- Свечи 1m с MEXC ----------
async def fetch_klines_1m(session: aiohttp.ClientSession, symbol: str, limit: int = 30):
    # формат: /klines?symbol=BTCUSDT&interval=1m&limit=30
    return await _fetch_json(session, f"{MEXC_API}/klines", symbol=symbol, interval="1m", limit=str(limit))

# ---------- RSI(14) ----------
def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None

    # Wilder's RSI
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

# ---------- Главный цикл сканера ----------
async def scanner_loop(bot, chat_id: int):
    global _sent_startup_ping
    if not _sent_startup_ping:
        try:
            await bot.send_message(chat_id=chat_id, text="🛰 Scanner online: MEXC 1m • RSI фильтр")
        except Exception:
            log.exception("Startup ping failed")
        _sent_startup_ping = True

    # лог конфигурации один раз при старте
    log.info(
        "CFG: pump=%.2f%% rsi_min=%s scan=%ds refresh=%ds quote=%s conc=%d cooldown=%ds",
        PUMP_THRESHOLD*100, RSI_MIN, SCAN_INTERVAL, SYMBOL_REFRESH_SEC, QUOTE, MAX_CONCURRENCY, COOLDOWN_SEC
    )

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    last_pairs_count: int | None = None
    last_pairs_announce_ts: float = 0.0

    while True:
        try:
            symbols, refreshed = await fetch_symbols()

            # уведомление об обновлении пар — только при реальном изменении и не чаще 1 раза в 10 минут
            now_ts = time.time()
            if refreshed and (last_pairs_count != len(symbols)) and (now_ts - last_pairs_announce_ts) > 600:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"🔄 Пары MEXC обновлены: {len(symbols)} (QUOTE={QUOTE})"
                    )
                    last_pairs_count = len(symbols)
                    last_pairs_announce_ts = now_ts
                except Exception:
                    log.exception("Pairs announce failed")

            if not symbols:
                await asyncio.sleep(5)
                continue

            async with aiohttp.ClientSession(headers=_HTTP_HEADERS, timeout=aiohttp.ClientTimeout(total=12)) as s:

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
                                # --- анти-спам по символу ---
                                now_local = time.time()
                                async with _last_sent_lock:
                                    last = _last_sent.get(sym, 0.0)
                                    if now_local - last < COOLDOWN_SEC:
                                        return
                                    _last_sent[sym] = now_local
                                # --- конец анти-спама ---

                                pct = round(change * 100, 2)
                                mexc_url = f"https://www.mexc.com/exchange/{sym.replace(QUOTE,'')}_{QUOTE}"
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
                            log.exception("scan error %s", sym)
                            return

                tasks = [asyncio.create_task(handle(sym)) for sym in symbols]
                await asyncio.gather(*tasks)

        except Exception:
            log.exception("scanner_loop tick failed")

        await asyncio.sleep(SCAN_INTERVAL)


