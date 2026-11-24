"""Pure-python technical indicators (no numpy needed)."""
from __future__ import annotations

import math
from typing import Sequence


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: Sequence[float], period: int) -> list[float]:
    out: list[float] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        out.append(s / min(i + 1, period))
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[float]:
    """Wilder RSI. Returns list aligned with closes (first values ~50)."""
    if len(closes) < 2:
        return [50.0] * len(closes)
    out = [50.0]
    avg_gain = avg_loss = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gain, loss = max(ch, 0.0), max(-ch, 0.0)
        if i <= period:
            avg_gain += gain / period
            avg_loss += loss / period
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out.append(100.0 if avg_gain > 0 else 50.0)
        else:
            out.append(100 - 100 / (1 + avg_gain / avg_loss))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[float]:
    trs = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[0] - lows[0])
        else:
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return ema(trs, period)


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0) -> tuple[list[float], list[float], list[float]]:
    mid = sma(closes, period)
    upper, lower = [], []
    for i in range(len(closes)):
        window = closes[max(0, i - period + 1): i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / len(window))
        upper.append(m + mult * sd)
        lower.append(m - mult * sd)
    return lower, mid, upper


def vwap(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    out, pv, vv = [], 0.0, 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes):
        tp = (h + l + c) / 3
        pv += tp * v
        vv += v
        out.append(pv / vv if vv else c)
    return out


def zscore(values: Sequence[float], lookback: int = 20) -> float:
    """Z-score of the last value against the previous ``lookback`` values."""
    if len(values) < 3:
        return 0.0
    window = list(values[-lookback - 1:-1])
    m = sum(window) / len(window)
    sd = math.sqrt(sum((x - m) ** 2 for x in window) / len(window))
    return (values[-1] - m) / sd if sd > 0 else (3.0 if values[-1] > m else 0.0)


def pct(a: float, b: float) -> float:
    """Percent change from b to a."""
    return (a - b) / b * 100 if b else 0.0
