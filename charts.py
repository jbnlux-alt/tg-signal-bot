# charts.py
import io
import math
from typing import List, Tuple

import pandas as pd
import numpy as np
import mplfinance as mpf

def klines_to_df(klines: List[List]) -> pd.DataFrame:
    """
    MEXC /klines формат:
    [ openTime, open, high, low, close, volume, closeTime, ...]
    """
    if not klines:
        raise ValueError("Empty klines")
    df = pd.DataFrame(klines, columns=[
        "openTime","open","high","low","close","volume","closeTime","qav","numTrades","takerBase","takerQuote","ignore"
    ][:len(klines[0])])
    # приведение типов
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    if "volume" in df:
        df["volume"] = df["volume"].astype(float)
    # индекс — время закрытия свечи
    ts = (df["closeTime"] if "closeTime" in df else df["openTime"]).astype(np.int64) // 1000
    df.index = pd.to_datetime(ts, unit="s")
    df = df.rename(columns={"open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"})
    return df[["Open","High","Low","Close"] + (["Volume"] if "Volume" in df.columns else [])]

def _pivot_levels(df: pd.DataFrame, lookback: int = 3) -> List[Tuple[float, str, int]]:
    """
    Находим локальные экстремумы (пивоты).
    Возвращаем список (price, kind['H'|'L'], index)
    """
    highs = df["High"].to_numpy()
    lows  = df["Low"].to_numpy()
    out: List[Tuple[float,str,int]] = []
    n = len(df)
    L = lookback
    for i in range(L, n - L):
        window_h = highs[i-L:i+L+1]
        window_l = lows[i-L:i+L+1]
        if highs[i] == window_h.max():
            out.append((highs[i], "H", i))
        if lows[i] == window_l.min():
            out.append((lows[i], "L", i))
    return out

def _cluster_levels(pivots: List[Tuple[float,str,int]], tolerance_ratio: float = 0.002, max_levels: int = 6) -> List[float]:
    """
    Кластеризуем цены пивотов в уровни. Сливаем, если разница < tolerance_ratio (например, 0.2%).
    Вес = количество касаний + бонус за недавние касания.
    """
    levels: List[dict] = []  # dict(price, hits, last_idx)
    for price, kind, idx in pivots:
        placed = False
        for lv in levels:
            # относительная дистанция
            if abs(price - lv["price"]) / max(1e-9, lv["price"]) < tolerance_ratio:
                # апдейтим усреднением (чтобы не уезжало сильно)
                lv["price"] = (lv["price"] * lv["hits"] + price) / (lv["hits"] + 1)
                lv["hits"] += 1
                lv["last_idx"] = max(lv["last_idx"], idx)
                placed = True
                break
        if not placed:
            levels.append({"price": price, "hits": 1, "last_idx": idx})
    # скоринг: больше хитов и более свежие — выше
    for lv in levels:
        lv["score"] = lv["hits"] + (lv["last_idx"] / (len(pivots) + 1))
    levels.sort(key=lambda x: x["score"], reverse=True)
    return [round(lv["price"], 8) for lv in levels[:max_levels]]

def compute_sr_levels(df: pd.DataFrame, lookback: int = 3, tolerance_ratio: float = 0.002, max_levels: int = 6) -> List[float]:
    piv = _pivot_levels(df, lookback=lookback)
    return _cluster_levels(piv, tolerance_ratio=tolerance_ratio, max_levels=max_levels)

def render_chart_image(symbol: str, klines: List[List], sr_lookback: int = 3, sr_tol: float = 0.002, max_levels: int = 6) -> io.BytesIO:
    """
    Рисуем свечи + горизонтальные уровни. Возвращаем PNG в BytesIO.
    """
    df = klines_to_df(klines)
    levels = compute_sr_levels(df, lookback=sr_lookback, tolerance_ratio=sr_tol, max_levels=max_levels)

    # Готовим hlines для mplfinance
    hlines = dict(hlines=levels, colors=["#888888"]*len(levels), linestyle="--", linewidths=1)

    # Последняя цена — жирной линией
    last_close = df["Close"].iloc[-1]
    hlines["hlines"].append(last_close)
    hlines["colors"].append("#000000")
    hlines["linestyle"] = ["--"]*(len(levels)) + ["-."]
    hlines["linewidths"] = [1]*len(levels) + [1.5]

    buf = io.BytesIO()
    mpf.plot(
        df,
        type="candle",
        style="yahoo",
        tight_layout=True,
        xrotation=0,
        hlines=hlines,
        volume=False,
        figsize=(8, 5),
        title=f"{symbol} • 1m • S/R levels"
        ,
        savefig=dict(fname=buf, dpi=150, bbox_inches="tight")
    )
    buf.seek(0)
    return buf
