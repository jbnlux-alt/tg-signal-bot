# scanner.py — FUTURES ONLY (MEXC USDT-PERPS)
import os, asyncio, aiohttp, time, logging, json
from typing import List, Optional
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from charts import render_chart_image, klines_to_df, compute_sr_levels

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("scanner")

# ---------- ENV / ПРАВИЛА ----------
PUMP_THRESHOLD     = float(os.getenv("PUMP_THRESHOLD", "0.07"))   # 7% за 1м
RSI_MIN            = float(os.getenv("RSI_MIN", "70"))
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL", "60"))
SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", "86400")) # раз в сутки
MAX_CONCURRENCY    = int(os.getenv("MAX_CONCURRENCY", "8"))
COOLDOWN_SEC       = int(os.getenv("COOLDOWN_SEC", "900"))

# Риск-менеджмент
DEPOSIT_USDT       = float(os.getenv("DEPOSIT_USDT", "100"))       # дефолт 100
RISK_PER_TRADE_BPS = float(os.getenv("RISK_PER_TRADE_BPS", "10"))  # 0.1%
MARGIN_CAP_BPS     = float(os.getenv("MARGIN_CAP_BPS", "100"))     # ≤1% суммарно
MIN_NOTIONAL_USDT  = float(os.getenv("MIN_NOTIONAL_USDT", "5"))

# Фильтры
MIN_COIN_AGE_DAYS  = int(os.getenv("MIN_COIN_AGE_DAYS", "30"))
REQUIRE_MONTHLY_DOWNTREND = os.getenv("REQUIRE_MONTHLY_DOWNTREND","true").lower()=="true"
ABNORMAL_DAILY_PUMP_BPS   = float(os.getenv("ABNORMAL_DAILY_PUMP_BPS","5000"))
BTC_FILTER         = os.getenv("BTC_FILTER","on").lower()=="on"
FUNDING_MAX_BPS    = float(os.getenv("FUNDING_MAX_BPS","30"))      # 0.30%

# Вход / стоп / тейк
ENTRY_MODE         = os.getenv("ENTRY_MODE","retest_sr")           # break1m | retest_sr
ENTRY_OFFSET_BPS   = float(os.getenv("ENTRY_OFFSET_BPS","5"))
STOP_MODE          = os.getenv("STOP_MODE","swing_high")           # swing_high | atr
STOP_BUFFER_BPS    = float(os.getenv("STOP_BUFFER_BPS","10"))
TAKE_PROFIT_R      = float(os.getenv("TAKE_PROFIT_R","2.5"))
ATR_PERIOD         = int(os.getenv("ATR_PERIOD","14"))
ATR_MULT           = float(os.getenv("ATR_MULT","1.5"))
DCA1_BPS           = float(os.getenv("DCA1_BPS","1000"))           # -10%
DCA2_BPS           = float(os.getenv("DCA2_BPS","1500"))           # -15%

# VIP/статистика
REQUIRE_VIP_STATS  = os.getenv("REQUIRE_VIP_STATS","false").lower()=="true"
STATS_FILE         = os.getenv("STATS_FILE","stats.json")

# ---------- API ----------
SPOT_API      = "https://api.mexc.com/api/v3"
OPEN_API      = "https://www.mexc.com/open/api/v2"
CONTRACT_API  = "https://contract.mexc.com/api/v1/contract"
QUOTE         = "USDT"  # для ссылок/маппинга

HTTP_TIMEOUT  = aiohttp.ClientTimeout(total=15)
HEADERS       = {"User-Agent":"TradeSignalFilterBot/2.0 (+render)", "Accept":"application/json"}

# ---------- КЭШ / СОСТОЯНИЕ ----------
_last_sent: dict[str, float] = {}
_last_sent_lock = asyncio.Lock()

# список фьючерсных символов (в формате SPOT: BTCUSDT). Кэш на сутки.
_fut_syms: List[str] = []
_last_refresh = 0.0
_next_fetch_at = 0.0

# Учёт «открытых» сигналов (для лимитов): очень простой в памяти
_open: List[dict] = []  # {sym, ts, notional}

def _prune_open(now: float, ttl: int = 6*3600):
    global _open
    _open = [x for x in _open if now - x["ts"] < ttl]

def _open_count_total() -> int:
    return len(_open)

def _open_count_symbol(sym: str) -> int:
    return sum(1 for x in _open if x["sym"] == sym)

def _open_margin_bps() -> float:
    total_notional = sum(x["notional"] for x in _open)
    return 10000.0 * total_notional / max(1e-9, DEPOSIT_USDT)

# ---------- УТИЛЫ ----------
async def _get_json(session: aiohttp.ClientSession, url: str, **params):
    async with session.get(url, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True) as r:
        r.raise_for_status()
        return await r.json(content_type=None)

def spot_to_contract(sym: str) -> str:
    # BTCUSDT → BTC_USDT
    return f"{sym.replace(QUOTE,'')}_{QUOTE}"

def mexc_futures_url(sym: str) -> str:
    base = sym.replace(QUOTE,'')
    return f"https://www.mexc.com/exchange/{base}_{QUOTE}?type=perpetual"

def tv_url(sym: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol=MEXC:{sym}"

# ---------- УНИВЕРС: ТОЛЬКО ФЬЮЧЕРСЫ ----------
async def _spot_usdt_candidates(session: aiohttp.ClientSession) -> List[str]:
    """Собираем кандидатов со спота (как список тикеров вида BTCUSDT)."""
    # 1) exchangeInfo
    try:
        info = await _get_json(session, f"{SPOT_API}/exchangeInfo")
        raw = info.get("symbols") or []
        out = [x["symbol"] for x in raw if x.get("status")=="TRADING" and x.get("quoteAsset")==QUOTE]
        if out: return sorted(set(out))
    except Exception:
        pass
    # 2) /ticker/price
    try:
        prices = await _get_json(session, f"{SPOT_API}/ticker/price")
        out = [it["symbol"] for it in prices if isinstance(it,dict) and str(it.get("symbol","")).endswith(QUOTE)]
        return sorted(set(out))
    except Exception:
        pass
    # 3) open/api/v2 (BTC_USDT → BTCUSDT)
    try:
        j = await _get_json(session, f"{OPEN_API}/market/symbols")
        data = j.get("data") or []
        res = []
        for it in data:
            st = (it.get("state") or "").upper()
            if st in ("ENABLED","ENALBED","ONLINE"):
                s = it.get("symbol","")
                if "_" in s:
                    b,q = s.split("_",1)
                    if q==QUOTE: res.append(f"{b}{q}")
        return sorted(set(res))
    except Exception:
        return []

async def _contract_exists(session: aiohttp.ClientSession, sym: str) -> bool:
    """Проверяем, что у SPOT-символа есть USDT-перп контракт."""
    c = spot_to_contract(sym)
    endpoints = [
        ("detail", {"symbol": c}),
        ("ticker", {"symbol": c}),
        ("indexPrice", {"symbol": c}),
    ]
    for ep, params in endpoints:
        try:
            j = await _get_json(session, f"{CONTRACT_API}/{ep}", **params)
            if isinstance(j, dict) and j.get("data") not in (None, []):
                return True
        except Exception:
            continue
    return False

async def fetch_futures_symbols() -> tuple[List[str], bool]:
    """Возвращает список спотовых тикеров, у которых есть USDT-perp контракт."""
    global _fut_syms, _last_refresh, _next_fetch_at
    now = time.time()
    if now < _next_fetch_at:
        return _fut_syms, False
    if _fut_syms and (now - _last_refresh) < SYMBOL_REFRESH_SEC:
        return _fut_syms, False

    try:
        async with aiohttp.ClientSession() as s:
            spot = await _spot_usdt_candidates(s)
            if not spot:
                _next_fetch_at = now + 300
                log.warning("No spot candidates; futures universe not refreshed (backoff 5m).")
                return _fut_syms, False

            sem = asyncio.Semaphore(20)
            fut: List[str] = []
            async def check(sym: str):
                async with sem:
                    if await _contract_exists(s, sym):
                        fut.append(sym)

            await asyncio.gather(*(check(sym) for sym in spot))
            fut = sorted(set(fut))
            if fut:
                _fut_syms = fut
                _last_refresh = now
                _next_fetch_at = now + SYMBOL_REFRESH_SEC
                log.info("Futures universe updated: %d symbols (USDT perps).", len(_fut_syms))
                return _fut_syms, True
            else:
                _next_fetch_at = now + 300
                log.warning("Contract check returned 0 symbols (backoff 5m).")
                return _fut_syms, False
    except Exception as e:
        _next_fetch_at = now + 300
        log.warning("fetch_futures_symbols failed: %s (backoff 5m).", e)
        return _fut_syms, False

# ---------- ДАННЫЕ / ИНДИКАТОРЫ ----------
async def spot_klines_1m(session: aiohttp.ClientSession, sym: str, limit: int = 180):
    # контрактные свечи бывают нестабильны по API — используем спот-цены как прокси для RSI/пампа/графика
    return await _get_json(session, f"{SPOT_API}/klines", symbol=sym, interval="1m", limit=str(limit))

async def get_24h_contract(session: aiohttp.ClientSession, sym: str) -> tuple[Optional[float], Optional[float]]:
    # 24h (перп): quoteVol + priceChangePercent
    c = spot_to_contract(sym)
    for ep in ("ticker", "detail"):
        try:
            j = await _get_json(session, f"{CONTRACT_API}/{ep}", symbol=c)
            d = j.get("data") or {}
            qv = d.get("quoteVol") or d.get("turnover") or d.get("quoteVolume")
            pch = d.get("riseFallRate") or d.get("priceChangePercent")
            return (float(qv) if qv is not None else None,
                    float(pch) if pch is not None else None)
        except Exception:
            continue
    return None, None

async def get_funding_rate(session: aiohttp.ClientSession, sym: str) -> Optional[float]:
    c = spot_to_contract(sym)
    for ep in ("fundingRate", "funding/prevRate"):
        try:
            j = await _get_json(session, f"{CONTRACT_API}/{ep}", symbol=c)
            d = j.get("data", j)
            if isinstance(d, dict):
                r = d.get("fundingRate") or d.get("rate")
                return float(r) if r is not None else None
            if isinstance(d, list) and d:
                r = d[0].get("fundingRate") or d[0].get("rate")
                return float(r) if r is not None else None
        except Exception:
            continue
    return None

async def get_max_leverage(session: aiohttp.ClientSession, sym: str) -> Optional[int]:
    c = spot_to_contract(sym)
    try:
        j = await _get_json(session, f"{CONTRACT_API}/detail", symbol=c)
        d = j.get("data") or {}
        lev = d.get("maxLeverage") or d.get("max_leverage")
        return int(lev) if lev is not None else None
    except Exception:
        return None

def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1: return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i]-closes[i-1]
        if d >= 0: gains += d
        else: losses += -d
    ag, al = gains/period, losses/period
    for i in range(period + 1, len(closes)):
        d = closes[i]-closes[i-1]
        g = d if d>0 else 0.0
        l = -d if d<0 else 0.0
        ag = (ag*(period-1)+g)/period
        al = (al*(period-1)+l)/period
    if al == 0: return 100.0
    rs = ag/al
    return 100 - (100/(1+rs))

def calc_atr(high: List[float], low: List[float], close: List[float], period: int = 14) -> Optional[float]:
    n = len(close)
    if n < period + 1: return None
    trs, pc = [], close[0]
    for i in range(1, n):
        tr = max(high[i]-low[i], abs(high[i]-pc), abs(low[i]-pc))
        trs.append(tr); pc = close[i]
    atr = sum(trs[:period])/period
    for i in range(period, len(trs)):
        atr = (atr*(period-1)+trs[i])/period
    return atr

def _slope(vals: List[float]) -> float:
    n = len(vals)
    if n < 2: return 0.0
    sx = n*(n-1)/2
    sx2 = (n-1)*n*(2*n-1)/6
    sy = sum(vals)
    sxy= sum(i*v for i,v in enumerate(vals))
    den = n*sx2 - sx*sx
    return 0.0 if den==0 else (n*sxy - sx*sy)/den

# Фильтры стратегии
async def coin_age_ok(session: aiohttp.ClientSession, sym: str) -> bool:
    d1 = await _get_json(session, f"{SPOT_API}/klines", symbol=sym, interval="1d", limit=str(MIN_COIN_AGE_DAYS+5))
    return isinstance(d1, list) and len(d1) >= MIN_COIN_AGE_DAYS

async def monthly_downtrend(session: aiohttp.ClientSession, sym: str) -> bool:
    if not REQUIRE_MONTHLY_DOWNTREND: return True
    d1 = await _get_json(session, f"{SPOT_API}/klines", symbol=sym, interval="1d", limit="40")
    if not isinstance(d1, list) or len(d1) < 25: return False
    closes = [float(x[4]) for x in d1][-30:]
    if len(closes) < 20: return False
    return _slope(closes) < 0

async def daily_pump_risk(session: aiohttp.ClientSession, sym: str) -> bool:
    d1 = await _get_json(session, f"{SPOT_API}/klines", symbol=sym, interval="1d", limit="60")
    if not isinstance(d1, list) or len(d1) < 2: return False
    c = [float(x[4]) for x in d1]
    thr = ABNORMAL_DAILY_PUMP_BPS/10000.0
    return any(abs((c[i]-c[i-1])/max(1e-12,c[i-1])) >= thr for i in range(1,len(c)))

async def btc_ok(session: aiohttp.ClientSession) -> bool:
    """BTC в коррекции/флэте: 15m изменение ≤0.5% ИЛИ 60m наклон ≤0.
    На MEXC часовой интервал — '60m'."""
    if not BTC_FILTER:
        return True
    try:
        m5 = await _get_json(session, f"{SPOT_API}/klines", symbol="BTCUSDT", interval="5m", limit="24")
        if not isinstance(m5, list) or len(m5) < 4:
            return True
        c5 = [float(x[4]) for x in m5]
        chg15 = (c5[-1] - c5[-4]) / c5[-4]
    except Exception:
        return True
    slope60 = 0.0
    try:
        h1 = await _get_json(session, f"{SPOT_API}/klines", symbol="BTCUSDT", interval="60m", limit="30")
        if isinstance(h1, list) and len(h1) >= 10:
            c60 = [float(x[4]) for x in h1][-20:]
            slope60 = _slope(c60)
    except Exception:
        pass
    return (abs(chg15) <= 0.005) or (slope60 <= 0)

# Entry/Stop/Take (SHORT)
def pick_short_entry(high: List[float], low: List[float], close: List[float], levels: List[float]) -> tuple[float,float,float,str]:
    last_c = close[-1]; prev_low = low[-2]; prev_high = high[-2]
    off = ENTRY_OFFSET_BPS/10000.0; buf = STOP_BUFFER_BPS/10000.0
    label = "PLAN"
    if ENTRY_MODE == "break1m":
        if last_c < prev_low:
            entry = last_c; label="NOW"
        else:
            entry = prev_low*(1-off)
    else:
        above = [lv for lv in levels if lv >= last_c]
        entry = (min(above) if above else prev_high)*(1-off)
    if STOP_MODE=="atr":
        atr = calc_atr(high, low, close, ATR_PERIOD) or 0.0
        stop = entry + atr*ATR_MULT
    else:
        stop = max(high[-1], high[-2])*(1+buf)
    if stop <= entry: stop = entry*(1+max(buf,0.001))
    risk = stop-entry
    take = entry - TAKE_PROFIT_R*risk
    return round(entry,8), round(stop,8), round(take,8), label

def position_size(entry: float, stop: float) -> tuple[float,float,str]:
    risk_usdt = DEPOSIT_USDT*(RISK_PER_TRADE_BPS/10000.0)
    note = "0.1% от депозита"
    risk = stop-entry
    if risk <= 0: return 0.0, 0.0, "Некорректный стоп/вход"
    qty = risk_usdt/risk
    notional = qty*entry
    if notional < MIN_NOTIONAL_USDT:
        risk_usdt = DEPOSIT_USDT*(max(RISK_PER_TRADE_BPS,30)/10000.0)  # 0.3%
        qty = risk_usdt/risk; notional = qty*entry; note = "0.3% от депозита (для мин. ордера/усреднений)"
    return round(notional,2), round(qty,6), note

# VIP / статистика
def load_stats() -> dict:
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def vip_flag(stats: dict, sym: str) -> bool:
    s = stats.get(sym)
    if not s: return False
    closed, wins = int(s.get("closed",0)), int(s.get("wins",0))
    wr = (wins/closed) if closed else 0.0
    return closed>=20 and wr>=0.90

def fmt_stats(s: Optional[dict]) -> str:
    if not s: return "Статистика: нет данных."
    p15, p4h, p24h, p3d = s.get("p15m",0), s.get("p4h",0), s.get("p24h",0), s.get("p3d",0)
    nohit, closed, wins = s.get("nohit",0), s.get("closed",0), s.get("wins",0)
    wr = (wins/closed*100) if closed else 0.0
    return (f"Статистика прибыльных:\n"
            f"• За 15 мин: {p15}\n• За 4 часа: {p4h}\n• За 24 часа: {p24h}\n• За 3 дня: {p3d}\n"
            f"Без отработки: {nohit}\nВинрейт: {wr:.2f}%")

# ---------- ОСНОВНОЙ ЦИКЛ ----------
async def scanner_loop(bot, chat_id: int):
    # приветствие
    await bot.send_message(chat_id=chat_id, text="🛰 Scanner online: MEXC Futures (USDT-perps) • RSI/SR")

    log.info("CFG: pump=%.2f%% rsi_min=%s scan=%ds R=%.2f entry=%s deposit=%.2f",
             PUMP_THRESHOLD*100, RSI_MIN, SCAN_INTERVAL, TAKE_PROFIT_R, ENTRY_MODE, DEPOSIT_USDT)

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    last_universe_count = -1
    last_announce_ts = 0.0
    stats_all = load_stats()

    while True:
        try:
            syms, refreshed = await fetch_futures_symbols()
            now = time.time()
            if refreshed and (last_universe_count != len(syms)) and (now - last_announce_ts) > 600:
                await bot.send_message(chat_id=chat_id, text=f"🔄 Фьючерсный универс обновлён: {len(syms)} USDT-перпов")
                last_universe_count = len(syms); last_announce_ts = now

            if not syms:
                await asyncio.sleep(10); continue

            async with aiohttp.ClientSession() as session:

                # BTC фильтр (общий) — быстрый выход
                if not await btc_ok(session):
                    await asyncio.sleep(SCAN_INTERVAL); continue

                async def worker(sym: str):
                    async with sem:
                        try:
                            # Возраст / тренд / риск дневных пампов / VIP
                            if not await coin_age_ok(session, sym): return
                            risk_pumps = await daily_pump_risk(session, sym)
                            if REQUIRE_MONTHLY_DOWNTREND and not await monthly_downtrend(session, sym): return
                            if REQUIRE_VIP_STATS and not vip_flag(stats_all, sym): return

                            # 1m спот-свечи как прокси
                            m1 = await spot_klines_1m(session, sym, 180)
                            if not isinstance(m1, list) or len(m1) < 20: return
                            closes = [float(x[4]) for x in m1]
                            highs  = [float(x[2]) for x in m1]
                            lows   = [float(x[3]) for x in m1]
                            prev_c, last_c = closes[-2], closes[-1]
                            if prev_c <= 0: return

                            change = (last_c - prev_c) / prev_c
                            rsi = calc_rsi(closes, 14)
                            if rsi is None or change < PUMP_THRESHOLD or rsi < RSI_MIN: return

                            # Уровни S/R → вход/стоп/тейк
                            df = klines_to_df(m1[-120:])
                            srl = compute_sr_levels(df, lookback=3, tolerance_ratio=0.002, max_levels=6)
                            entry, stop, take, label = pick_short_entry(highs, lows, closes, srl)
                            notional, qty, note = position_size(entry, stop)

                            # Лимиты по открытым и марже
                            _prune_open(time.time())
                            future_margin_bps = _open_margin_bps() + 10000.0*notional/max(1e-9,DEPOSIT_USDT)
                            if _open_count_total() >= 3 or _open_count_symbol(sym) >= 2 or future_margin_bps > MARGIN_CAP_BPS:
                                return

                            # Анти-спам
                            async with _last_sent_lock:
                                last = _last_sent.get(sym, 0.0)
                                if time.time() - last < COOLDOWN_SEC: return
                                _last_sent[sym] = time.time()
                            _open.append({"sym": sym, "ts": time.time(), "notional": notional})

                            # Контрактная инфа
                            vol24, _ = await get_24h_contract(session, sym)
                            fund = await get_funding_rate(session, sym)
                            lev  = await get_max_leverage(session, sym)

                            fund_warn = (fund is not None and abs(fund) > (FUNDING_MAX_BPS/10000.0))

                            # Безопасные строки для форматирования
                            lev_str  = f"x{lev}" if lev else "—"
                            vol_str  = f"~${round(vol24/1e6, 2)}M" if vol24 else "—"
                            fund_str = f"{fund*100:.4f}%" if fund is not None else "n/a"

                            # Рендер графика
                            try:
                                img = render_chart_image(sym, m1)
                            except Exception:
                                log.exception("chart render %s", sym)
                                img = None

                            vip = vip_flag(stats_all, sym)
                            s_sym = stats_all.get(sym)

                            header = "#VIP⭐\n\n" if vip else ""
                            header += f"{sym} — PUMP +{round(change*100,2)}% ({prev_c} → {last_c})\n"
                            lines = [
                                f"Плечо: {lev_str} • рекоменд. ≤50×",
                                f"Макс. вход: ${notional:.2f}",
                                f"Объём 24h: {vol_str}",
                                f"Фандинг: {fund_str}",
                                f"RSI: {rsi:.2f}",
                                f"Открытых позиций: всего {_open_count_total()} • по {sym}: {_open_count_symbol(sym)}",
                                "",
                                f"ENTRY: <b>{entry}</b>   STOP: <b>{stop}</b>   TAKE: <b>{take}</b>  (~{TAKE_PROFIT_R:.1f}R)",
                                f"Риск: {note} → ~<b>{notional} USDT</b> (≈ {qty} {sym.replace(QUOTE,'')})",
                                f"DCA: -{int(DCA1_BPS/10)}‰ / -{int(DCA2_BPS/10)}‰ (после усреднения — тейк 200–250%)",
                                "",
                                (fmt_stats(s_sym) if s_sym else "Статистика: нет данных.")
                            ]
                            warns = []
                            if risk_pumps: warns.append("⚠️ Бывали суточные пампы ≥50% — высокий риск, готовь DCA.")
                            if fund_warn:  warns.append("⚠️ Высокий фандинг — избегай или закрывайся в б/у за 10 мин до расчёта.")
                            if warns: lines += ["", *warns]

                            caption = header + "\n".join(lines)

                            kb = InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔘 MEXC Futures", url=mexc_futures_url(sym)),
                                 InlineKeyboardButton("📈 TradingView", url=tv_url(sym))]
                            ])

                            if img:
                                await bot.send_photo(chat_id=chat_id, photo=img, caption=caption,
                                                     reply_markup=kb, parse_mode=ParseMode.HTML)
                            else:
                                await bot.send_message(chat_id=chat_id, text=caption+"\n"+mexc_futures_url(sym),
                                                       reply_markup=kb, parse_mode=ParseMode.HTML,
                                                       disable_web_page_preview=True)
                        except Exception:
                            log.exception("worker error %s", sym)

                await asyncio.gather(*(worker(s) for s in syms))

        except Exception:
            log.exception("scanner_loop tick failed")

        await asyncio.sleep(SCAN_INTERVAL)
