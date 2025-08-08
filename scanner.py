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

# ---------- ENV (настраивается в Render → Environment) ----------
PUMP_THRESHOLD     = float(os.getenv("PUMP_THRESHOLD", "0.07"))     # 7% за 1м
RSI_MIN            = float(os.getenv("RSI_MIN", "70"))              # порог RSI
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", "60"))          # сек между итерациями
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", "86400"))  # обновление списка пар (сек)
QUOTE              = os.getenv("QUOTE_FILTER", "USDT")              # котировка
MAX_CONCURRENCY    = int(os.getenv("MAX_CONCURRENCY", "8"))         # параллельные запросы

MEXC_API = "https://api.mexc.com/api/v3"

# ---------- Кэш списка пар ----------
_symbols_cache: list[str] = []
_last_reload: float = 0.0
_sent_startup_ping: bool = False

# ---------- HTTP helper ----------
async def _fetch_json(session: aiohttp.ClientSession, url: str, **params):
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        return await r.json()

# ---------- Загрузка всех USDT-пар MEXC (с бэк-оффом и без затирания кэша пустыми данными) ----------
async def fetch_symbols() -> tuple[list[str], bool]:
    """
    Возвращает (symbols, refreshed_now).
    refreshed_now=True — только если кэш реально обновился валидными данными.
    При пустом ответе/ошибке кэш сохраняем и ставим бэк-офф 5 минут.
    """
    global _symbols_cache, _last_reload
    now = time.time()

    # ещё рано обновлять — отдаем кэш
    if _symbols_cache and (now - _last_reload) < SYMBOL_REFRESH_SEC:
        return _symbols_cache, False

    try:
        async with aiohttp.ClientSession() as s:
            info = await _fetch_json(s, f"{MEXC_API}/exchangeInfo")
        syms: list[str] = []
        for x in info.get("symbols", []):
            if x.get("status") == "TRADING" and x.get("quoteAsset") == QUOTE:
                syms.append(x["symbol"])
        syms = sorted(set(syms))

        if syms:
            _symbols_cache = syms
            _last_reload = now
            log.info("Pairs updated: %d (QUOTE=%s)", len(_symbols_cache), QUOTE)
            return _symbols_cache, True
        else:
            # пустышка — кэш НЕ трогаем, следующий опрос не раньше чем через 5 минут
            log.warning("MEXC returned 0 symbols; keep cache=%d. Backoff 5m.", len(_symbols_cache))
            _last_reload = now - SYMBOL_REFRESH_SEC + 300
            return _symbols_cache, False

    except Exception as e:
        # сетевая/HTTP ошибка — кэш сохраняем, бэк-офф 5 минут
        log.exception("Pairs refresh failed: %s", e)
        _last_reload = now - SYMBOL_REFRESH_SEC + 300
        return _symbols_cache, False

# ---------- Свечи 1m с MEXC ----------
async def fetch_klines_1m(session: aiohttp.ClientSession, symbol: str, limit: int = 30):
    # формат: /klines?symbol=BTCUSDT&interval=1m&limit=30
    return await _fetch_json(session, f"{MEXC_API}/klines", symbol=symbol, interval="1m", limit=str(limit))

# ---------- RSI(14) ----------
def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None

    # Wilder's RSI: начальные средние по первым 'period' изменениям
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

            # уведомление об обновлении пар — только при реальном изменении и не чаще, чем раз в 10 минут
            now = time.time()
            if refreshed:
                if last_pairs_count != len(symbols) and (now - last_pairs_announce_ts) > 600:
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"🔄 Пары MEXC обновлены: {len(symbols)} (QUOTE={QUOTE})"
                        )
                        last_pairs_count = len(symbols)
                        last_pairs_announce_ts = now
                    except Exception:
                        log.exception("Pairs announce failed")

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
                                # У TV не все тикеры MEXC есть — оставляем общий чарт с символом
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
            # не уронить цикл целиком
            log.exception("scanner_loop tick failed")

        await asyncio.sleep(SCAN_INTERVAL)


