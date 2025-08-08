# scanner.py
import os, asyncio, aiohttp, time, logging, math, json
from typing import List, Tuple, Optional
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from charts import render_chart_image, klines_to_df, compute_sr_levels

# ---------- Логи ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("scanner")

# ---------- Анти-спам по символу ----------
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "900"))
_last_sent: dict[str, float] = {}
_last_sent_lock = asyncio.Lock()

# ---------- ENV (правила) ----------
PUMP_THRESHOLD     = float(os.getenv("PUMP_THRESHOLD", "0.07"))     # 7%/1m
RSI_MIN            = float(os.getenv("RSI_MIN", "70"))
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", "60"))
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", "86400"))
QUOTE              = os.getenv("QUOTE_FILTER", "USDT")
MAX_CONCURRENCY    = int(os.getenv("MAX_CONCURRENCY", "8"))

# Риск-менеджмент и фильтры
DEPOSIT_USDT       = float(os.getenv("DEPOSIT_USDT", "100"))        # <— по умолчанию 100
RISK_PER_TRADE_BPS = float(os.getenv("RISK_PER_TRADE_BPS", "10"))   # 0.1% на сделку
OPEN_TRADE_TTL_SEC = int(os.getenv("OPEN_TRADE_TTL_SEC", "21600"))  # 6h в учёте
MAX_OPEN_TRADES    = int(os.getenv("MAX_OPEN_TRADES", "3"))
PER_SYMBOL_MAX_OPEN= int(os.getenv("PER_SYMBOL_MAX_OPEN", "2"))
MARGIN_CAP_BPS     = float(os.getenv("MARGIN_CAP_BPS", "100"))      # 1% суммарно
MIN_NOTIONAL_USDT  = float(os.getenv("MIN_NOTIONAL_USDT", "5"))

REQUIRE_VIP_STATS  = os.getenv("REQUIRE_VIP_STATS", "false").lower()=="true"
STATS_FILE         = os.getenv("STATS_FILE", "stats.json")

MIN_COIN_AGE_DAYS  = int(os.getenv("MIN_COIN_AGE_DAYS", "30"))
REQUIRE_MONTHLY_DOWNTREND = os.getenv("REQUIRE_MONTHLY_DOWNTREND","true").lower()=="true"
ABNORMAL_DAILY_PUMP_BPS   = float(os.getenv("ABNORMAL_DAILY_PUMP_BPS","5000"))  # 50%/day — риск

BTC_FILTER         = os.getenv("BTC_FILTER","on").lower()=="on"
FUNDING_MAX_BPS    = float(os.getenv("FUNDING_MAX_BPS","30"))  # 0.30% порог (если возьмём с перпов)

# Вход/стоп/тейк
ENTRY_MODE         = os.getenv("ENTRY_MODE", "retest_sr")      # <-- торгуем от зон
ENTRY_OFFSET_BPS   = float(os.getenv("ENTRY_OFFSET_BPS","5"))  # 0.05% подтяжка
STOP_MODE          = os.getenv("STOP_MODE","swing_high")       # или atr
STOP_BUFFER_BPS    = float(os.getenv("STOP_BUFFER_BPS","10"))  # 0.10%
TAKE_PROFIT_R      = float(os.getenv("TAKE_PROFIT_R","2.5"))   # 250%
ATR_PERIOD         = int(os.getenv("ATR_PERIOD","14"))
ATR_MULT           = float(os.getenv("ATR_MULT","1.5"))

# Усреднения (п.11) — бипсы (-10% и -15%)
DCA1_BPS           = float(os.getenv("DCA1_BPS","1000"))
DCA2_BPS           = float(os.getenv("DCA2_BPS","1500"))

MEXC_API      = "https://api.mexc.com/api/v3"
OPEN_API      = "https://www.mexc.com/open/api/v2"
CONTRACT_API  = "https://contract.mexc.com/api/v1/contract"

# ---------- Кэш пар и бэк-офф ----------
_symbols_cache: list[str] = []
_last_reload: float = 0.0
_next_pairs_fetch_at: float = 0.0
_sent_startup_ping: bool = False
_HTTP_HEADERS = { "User-Agent":"TradeSignalFilterBot/1.0 (+render)", "Accept":"application/json" }

# учёт открытых сделок (простой MVP) — список словарей
_open_trades: List[dict] = []  # {sym, ts, notional}
def _prune_open_trades(now: float):
    global _open_trades
    _open_trades = [t for t in _open_trades if now - t["ts"] < OPEN_TRADE_TTL_SEC]

def _count_total_open() -> int:
    return len(_open_trades)

def _count_open_for(sym: str) -> int:
    return sum(1 for t in _open_trades if t["sym"] == sym)

def _total_margin_used_bps() -> float:
    # очень грубо: считаем notional как маржу (без плеча)
    notional_sum = sum(t["notional"] for t in _open_trades)
    return 10000.0 * (notional_sum / max(1e-9, DEPOSIT_USDT))

def _can_open(sym: str, extra_notional: float) -> tuple[bool,str]:
    now = time.time()
    _prune_open_trades(now)
    if _count_total_open() >= MAX_OPEN_TRADES:
        return False, f"Достигнут лимит {MAX_OPEN_TRADES} открытых сделок"
    if _count_open_for(sym) >= PER_SYMBOL_MAX_OPEN:
        return False, f"По {sym} уже {PER_SYMBOL_MAX_OPEN} сделки(ок)"
    if (_total_margin_used_bps() + 10000.0*extra_notional/max(1e-9,DEPOSIT_USDT)) > MARGIN_CAP_BPS:
        return False, f"Суммарная маржа превысит {MARGIN_CAP_BPS/100:.2f}% депозита"
    return True, ""

def _mark_open(sym: str, notional: float):
    _open_trades.append({"sym": sym, "ts": time.time(), "notional": float(notional)})

# ---------- Статистика/VIP ----------
def load_stats() -> dict:
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def vip_flag(stats: dict, sym: str) -> bool:
    s = stats.get(sym)
    if not s: return False
    closed, wins = int(s.get("closed",0)), int(s.get("wins",0))
    winrate = (wins/closed) if closed else 0.0
    return closed >= 20 and winrate >= 0.90

def format_stat_block(s: Optional[dict]) -> str:
    if not s: return "Статистика: нет данных."
    p15  = s.get("p15m", 0)
    p4h  = s.get("p4h", 0)
    p24h = s.get("p24h", 0)
    p3d  = s.get("p3d", 0)
    nohit= s.get("nohit", 0)
    closed = s.get("closed", 0)
    wins   = s.get("wins", 0)
    wr = (wins/closed*100) if closed else 0.0
    return (f"Статистика прибыльных:\n"
            f"• За 15 мин: {p15}\n"
            f"• За 4 часа: {p4h}\n"
            f"• За 24 часа: {p24h}\n"
            f"• За 3 дня: {p3d}\n"
            f"Без отработки: {nohit}\n"
            f"Винрейт: {wr:.2f}%")

# ---------- HTTP ----------
async def _fetch_json(session: aiohttp.ClientSession, url: str, **params):
    async with session.get(url, params=params, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        return await r.json(content_type=None)

def to_contract_symbol(spot_sym: str) -> str:
    return f"{spot_sym.replace(QUOTE,'')}_{QUOTE}"

# 24h объём (SPOT)
async def get_24h_stats(session: aiohttp.ClientSession, sym: str) -> tuple[Optional[float], Optional[float]]:
    try:
        j = await _fetch_json(session, f"{MEXC_API}/ticker/24hr", symbol=sym)
        qv = float(j.get("quoteVolume")) if isinstance(j, dict) and j.get("quoteVolume") is not None else None
        pch= float(j.get("priceChangePercent")) if isinstance(j, dict) and j.get("priceChangePercent") is not None else None
        return qv, pch
    except Exception:
        return None, None

# Контрактная инфа (макс. плечо) — best-effort
async def get_max_leverage(session: aiohttp.ClientSession, sym: str) -> Optional[int]:
    c = to_contract_symbol(sym)
    try:
        j = await _fetch_json(session, f"{CONTRACT_API}/detail", symbol=c)
        data = j.get("data") or {}
        lev = data.get("maxLeverage") or data.get("max_leverage")
        return int(lev) if lev is not None else None
    except Exception:
        return None

# Фандинг (последний/текущий) — best-effort
async def get_funding_info(session: aiohttp.ClientSession, sym: str) -> tuple[Optional[float], Optional[int]]:
    c = to_contract_symbol(sym)
    # пробуем несколько эндпоинтов
    paths = [
        "funding/prevRate",    # ?symbol=BTC_USDT
        "fundingRate",         # ?symbol=BTC_USDT
        "funding/prev_funding_rate"  # возможный вариант
    ]
    for p in paths:
        try:
            j = await _fetch_json(session, f"{CONTRACT_API}/{p}", symbol=c)
            data = j.get("data", j)
            if isinstance(data, dict):
                rate = data.get("fundingRate") or data.get("rate")
                next_ts = data.get("nextFundingTime") or data.get("nextTime")
                if rate is not None:
                    r = float(rate)  # доля, напр. 0.001 = 0.1%
                    nxt = int(next_ts) if next_ts is not None else None
                    return r, nxt
            if isinstance(data, list) and data:
                d = data[0]
                rate = d.get("fundingRate") or d.get("rate")
                next_ts = d.get("nextFundingTime") or d.get("nextTime")
                if rate is not None:
                    return float(rate), (int(next_ts) if next_ts is not None else None)
        except Exception:
            continue
    return None, None

# ---------- Пары ----------
async def fetch_symbols() -> tuple[list[str], bool]:
    global _symbols_cache, _last_reload, _next_pairs_fetch_at
    now = time.time()
    if now < _next_pairs_fetch_at:
        return _symbols_cache, False
    if (now - _last_reload) < SYMBOL_REFRESH_SEC and _symbols_cache:
        return _symbols_cache, False

    symbols: list[str] = []
    try:
        async with aiohttp.ClientSession(headers=_HTTP_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as s:
            # 1) exchangeInfo
            try:
                info = await _fetch_json(s, f"{MEXC_API}/exchangeInfo")
                raw = info.get("symbols") or []
                if raw:
                    symbols = [x["symbol"] for x in raw if x.get("status")=="TRADING" and x.get("quoteAsset")==QUOTE]
            except Exception as e:
                log.warning("exchangeInfo failed: %s", e)
            # 2) /ticker/price (фолбэк)
            if not symbols:
                try:
                    prices = await _fetch_json(s, f"{MEXC_API}/ticker/price")
                    cand = [it["symbol"] for it in prices if isinstance(it,dict) and it.get("symbol","").endswith(QUOTE)]
                    bad = ("3L","3S","4L","4S","5L","5S","UP","DOWN")
                    def ok(sym:str)->bool:
                        base = sym[:-len(QUOTE)] if sym.endswith(QUOTE) else sym
                        return not any(base.endswith(s) for s in bad)
                    symbols = [sym for sym in cand if ok(sym)]
                except Exception as e:
                    log.warning("ticker/price fallback failed: %s", e)
            # 3) /open/api/v2/market/symbols (фолбэк)
            if not symbols:
                try:
                    j = await _fetch_json(s, f"{OPEN_API}/market/symbols")
                    data = j.get("data") or []
                    conv = []
                    for it in data:
                        st = (it.get("state") or "").upper()
                        if st in ("ENABLED","ENALBED","ONLINE"):
                            sym = it.get("symbol","")
                            if "_" in sym:
                                base, quote = sym.split("_",1)
                                if quote==QUOTE: conv.append(f"{base}{quote}")
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
        _next_pairs_fetch_at = now + 300
        log.warning("MEXC returned 0 symbols. Backoff 5m.")
        return _symbols_cache, False

# ---------- Klines ----------
async def fetch_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int = 200):
    return await _fetch_json(session, f"{MEXC_API}/klines", symbol=symbol, interval=interval, limit=str(limit))

# ---------- Индикаторы ----------
def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1: return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0: gains += d
        else:      losses += -d
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = d if d > 0 else 0.0
        loss = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_atr(high: List[float], low: List[float], close: List[float], period: int = 14) -> Optional[float]:
    n = len(close)
    if n < period + 1: return None
    trs = []
    prev_close = close[0]
    for i in range(1, n):
        tr = max(high[i]-low[i], abs(high[i]-prev_close), abs(low[i]-prev_close))
        trs.append(tr)
        prev_close = close[i]
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr

# ---------- Фильтры стратегии ----------
def _slope(vals: List[float]) -> float:
    n = len(vals)
    if n < 2: return 0.0
    sx = n*(n-1)/2
    sx2 = (n-1)*n*(2*n-1)/6
    sy = sum(vals)
    sxy= sum(i*v for i,v in enumerate(vals))
    denom = n*sx2 - sx*sx
    if denom == 0: return 0.0
    return (n*sxy - sx*sy) / denom

async def coin_age_ok(session: aiohttp.ClientSession, sym: str) -> bool:
    d1 = await fetch_klines(session, sym, "1d", min(200, MIN_COIN_AGE_DAYS+5))
    return isinstance(d1, list) and len(d1) >= MIN_COIN_AGE_DAYS

async def monthly_downtrend(session: aiohttp.ClientSession, sym: str) -> bool:
    if not REQUIRE_MONTHLY_DOWNTREND: return True
    d1 = await fetch_klines(session, sym, "1d", 40)
    if not isinstance(d1, list) or len(d1) < 25: return False
    closes = [float(x[4]) for x in d1][-30:]
    if len(closes) < 20: return False
    return _slope(closes) < 0

async def daily_abnormal_pump_risk(session: aiohttp.ClientSession, sym: str) -> bool:
    d1 = await fetch_klines(session, sym, "1d", 60)
    if not isinstance(d1, list) or len(d1) < 2: return False
    closes = [float(x[4]) for x in d1]
    for i in range(1, len(closes)):
        chg = (closes[i]-closes[i-1]) / max(1e-12, closes[i-1])
        if abs(chg) >= ABNORMAL_DAILY_PUMP_BPS/10000.0:
            return True
    return False

async def btc_ok(session: aiohttp.ClientSession) -> bool:
    if not BTC_FILTER: return True
    d5 = await fetch_klines(session, "BTCUSDT", "5m", 24)
    if not isinstance(d5, list) or len(d5) < 4: return True
    closes5 = [float(x[4]) for x in d5]
    chg15 = (closes5[-1]-closes5[-4]) / closes5[-4]
    d1h = await fetch_klines(session, "BTCUSDT", "1h", 30)
    slope1h = 0.0
    if isinstance(d1h, list) and len(d1h) >= 10:
        closes1h = [float(x[4]) for x in d1h][-20:]
        slope1h = _slope(closes1h)
    return (abs(chg15) <= 0.005) or (slope1h <= 0)

# ---------- Entry/Stop/Take ----------
def pick_short_entry(high: List[float], low: List[float], close: List[float], levels: List[float]) -> tuple[float, float, float, str]:
    last_c = close[-1]
    prev_low = low[-2]
    prev_high= high[-2]
    offset = ENTRY_OFFSET_BPS/10000.0
    buf = STOP_BUFFER_BPS/10000.0

    label = "PLAN"
    if ENTRY_MODE=="break1m":
        if last_c < prev_low:
            entry = last_c; label="NOW"
        else:
            entry = prev_low * (1 - offset)
    else:  # retest_sr
        above = [lv for lv in levels if lv >= last_c]
        if above:
            sr = min(above); entry = sr * (1 - offset)
        else:
            entry = prev_high * (1 - offset)

    if STOP_MODE=="atr":
        atr = calc_atr(high, low, close, period=ATR_PERIOD) or 0.0
        stop = entry + atr*ATR_MULT
    else:
        sh = max(high[-1], high[-2])
        stop = sh * (1 + buf)
    if stop <= entry:
        stop = entry * (1 + max(buf, 0.001))
    risk = stop - entry
    take = entry - TAKE_PROFIT_R * risk
    return (round(entry,8), round(stop,8), round(take,8), label)

def position_size_usdt(entry: float, stop: float) -> tuple[float, float, str]:
    risk_usdt = DEPOSIT_USDT * (RISK_PER_TRADE_BPS / 10000.0)  # 0.1%
    note = "0.1% от депозита"
    price_risk = stop - entry
    if price_risk <= 0:
        return 0.0, 0.0, "Некорректный стоп/вход"
    qty = risk_usdt / price_risk
    notional = qty * entry
    if notional < MIN_NOTIONAL_USDT:
        risk_usdt = DEPOSIT_USDT * (max(RISK_PER_TRADE_BPS, 30) / 10000.0)  # 0.3%
        qty = risk_usdt / price_risk
        notional = qty * entry
        note = "0.3% от депозита (для мин. ордера/усреднений)"
    return (round(notional, 2), round(qty, 6), note)

# ---------- Главный цикл ----------
async def scanner_loop(bot, chat_id: int):
    global _sent_startup_ping
    if not _sent_startup_ping:
        try: await bot.send_message(chat_id=chat_id, text="🛰 Scanner online: MEXC 1m • RSI фильтр")
        except Exception: log.exception("Startup ping failed")
        _sent_startup_ping = True

    log.info(
        "CFG: pump=%.2f%% rsi_min=%s scan=%ds refresh=%ds quote=%s conc=%d cooldown=%ds R=%.2f entry=%s",
        PUMP_THRESHOLD*100, RSI_MIN, SCAN_INTERVAL, SYMBOL_REFRESH_SEC, QUOTE, MAX_CONCURRENCY, COOLDOWN_SEC, TAKE_PROFIT_R, ENTRY_MODE
    )

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    last_pairs_count: int | None = None
    last_pairs_announce_ts: float = 0.0
    stats_all = load_stats()

    while True:
        try:
            symbols, refreshed = await fetch_symbols()
            now_ts = time.time()
            if refreshed and (last_pairs_count != len(symbols)) and (now_ts - last_pairs_announce_ts) > 600:
                try:
                    await bot.send_message(chat_id=chat_id, text=f"🔄 Пары MEXC обновлены: {len(symbols)} (QUOTE={QUOTE})")
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
                            # Фильтры до тяжёлых расчётов
                            if REQUIRE_VIP_STATS and not vip_flag(stats_all, sym):
                                return
                            if not await btc_ok(s):  # п.7
                                return
                            if not await coin_age_ok(s, sym):  # п.2
                                return
                            risk_pumps = await daily_abnormal_pump_risk(s, sym)  # п.18 (флаг)
                            if REQUIRE_MONTHLY_DOWNTREND and not await monthly_downtrend(s, sym):  # п.10
                                return

                            # Данные 1m
                            m1 = await fetch_klines(s, sym, "1m", 180)
                            if not isinstance(m1, list) or len(m1) < 20: return
                            closes = [float(x[4]) for x in m1]
                            highs  = [float(x[2]) for x in m1]
                            lows   = [float(x[3]) for x in m1]
                            prev_c, last_c = closes[-2], closes[-1]
                            if prev_c <= 0: return

                            change = (last_c - prev_c) / prev_c
                            rsi = calc_rsi(closes, period=14)
                            if rsi is None: return
                            if change < PUMP_THRESHOLD or rsi < RSI_MIN:
                                return

                            # Уровни S/R
                            df = klines_to_df(m1[-120:])
                            sr_levels = compute_sr_levels(df, lookback=3, tolerance_ratio=0.002, max_levels=6)
                            entry, stop, take, label = pick_short_entry(highs, lows, closes, sr_levels)
                            notional, qty, size_note = position_size_usdt(entry, stop)

                            # Лимиты по открытым сделкам и суммарной марже
                            ok, msg = _can_open(sym, notional)
                            if not ok:
                                log.info("Skip %s: %s", sym, msg)
                                return
                            # Анти-спам
                            now_local = time.time()
                            async with _last_sent_lock:
                                last = _last_sent.get(sym, 0.0)
                                if now_local - last < COOLDOWN_SEC:
                                    return
                                _last_sent[sym] = now_local
                            # помечаем "открытой" (для лимитов) — это именно сигнал, но учёт маржи работает
                            _mark_open(sym, notional)

                            # Доп. инфа: объём, фандинг, макс плечо
                            vol24, pch24 = await get_24h_stats(s, sym)
                            fund_rate, next_fund_ts = await get_funding_info(s, sym)
                            max_lev = await get_max_leverage(s, sym)

                            fund_ok = True
                            if fund_rate is not None:
                                if abs(fund_rate) > (FUNDING_MAX_BPS/10000.0):
                                    fund_ok = False

                            # Рендер графика
                            try:
                                img = render_chart_image(sym, m1)
                            except Exception:
                                log.exception("chart render failed %s", sym)
                                img = None

                            mexc_url = f"https://www.mexc.com/exchange/{sym.replace(QUOTE,'')}_{QUOTE}"
                            tv_url   = f"https://www.tradingview.com/chart/?symbol=MEXC:{sym}"
                            pct = round(change * 100, 2)

                            s_sym = stats_all.get(sym)
                            vip = vip_flag(stats_all, sym)

                            # Хедер как на скрине
                            header = "#VIP⭐\n\n" if vip else ""
                            header += f"{sym} — PUMP +{pct}% ({prev_c} → {last_c})\n"
                            lev_line = f"Плечо: {('x'+str(max_lev)) if max_lev else '—'} • рекоменд. ≤50×"
                            max_in_line = f"Макс. вход: ${notional:.2f}"
                            vol_line = f"Объём 24h: ${vol24/1e6:.2f}M" if vol24 else "Объём 24h: —"
                            fund_line = f"Фандинг: {(fund_rate*100):.4f}%" if fund_rate is not None else "Фандинг: n/a"
                            rsi_line  = f"RSI: {rsi:.2f}"

                            open_line = f"Открытых позиций: всего {_count_total_open()} • по {sym}: {_count_open_for(sym)}"

                            entry_block = (
                                f"ENTRY: <b>{entry}</b>   STOP: <b>{stop}</b>   TAKE: <b>{take}</b>  (~{TAKE_PROFIT_R:.1f}R)\n"
                                f"Риск: {size_note} → ~<b>{notional} USDT</b> (≈ {qty} {sym.replace(QUOTE,'')})\n"
                                f"DCA: -{int(DCA1_BPS/10)}‰ / -{int(DCA2_BPS/10)}‰ (после усреднения — переставь тейк на 200–250%)"
                            )

                            warns = []
                            if risk_pumps: warns.append("⚠️ За месяц были суточные пампы ≥50% — возможны глубокие усреднения.")
                            if not fund_ok: warns.append("⚠️ Высокий фандинг — лучше избегать или закрыть в б/у за 10 мин до финансирования.")
                            if _count_total_open() > 3: warns.append("⚠️ Больше 3 открытых сделок — по правилам нельзя.")
                            if _count_open_for(sym) >= 2: warns.append(f"⚠️ По {sym} уже {_count_open_for(sym)} сделки(ок).")
                            if _total_margin_used_bps() > MARGIN_CAP_BPS: warns.append("⚠️ Суммарная маржа >1% депозита.")

                            caption = (
                                header +
                                f"{lev_line}\n{max_in_line}\n{vol_line}\n{fund_line}\n{rsi_line}\n\n" +
                                f"{open_line}\n\n" +
                                entry_block + "\n\n" +
                                (format_stat_block(s_sym) if s_sym else "Статистика: нет данных.")
                            )
                            if warns:
                                caption += "\n\n" + "\n".join(warns)

                            kb = InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔘 MEXC", url=mexc_url),
                                 InlineKeyboardButton("📈 TradingView", url=tv_url)],
                            ])

                            if img:
                                await bot.send_photo(chat_id=chat_id, photo=img, caption=caption, reply_markup=kb, parse_mode=ParseMode.HTML)
                            else:
                                await bot.send_message(chat_id=chat_id, text=caption + f"\n🔗 MEXC: {mexc_url}\n📈 TV: {tv_url}",
                                                       reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

                        except Exception:
                            log.exception("scan error %s", sym)
                            return

                tasks = [asyncio.create_task(handle(sym)) for sym in symbols]
                await asyncio.gather(*tasks)

        except Exception:
            log.exception("scanner_loop tick failed")

        await asyncio.sleep(SCAN_INTERVAL)


