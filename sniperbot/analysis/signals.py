"""Chart research: turns candles + live pair stats into buy/sell decisions.

Four entry strategies, each scored 0-100:
  * early     - brand-new launch with strong buy pressure and healthy volume
  * momentum  - established uptrend: EMA stack, RSI in the power zone, rising volume
  * dip       - pullback inside an uptrend, oversold RSI turning back up
  * breakout  - close above recent range high on a volume spike

The highest-scoring enabled strategy wins. Exit analysis looks for trend breaks,
momentum exhaustion and sell-side pressure (dumps).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..data.market import Candle, PairInfo
from .indicators import bollinger, ema, pct, rsi, vwap, zscore


@dataclass
class StrategyScore:
    name: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def add(self, points: float, reason: str) -> None:
        self.score += points
        if points:
            self.reasons.append(f"{'+' if points > 0 else ''}{points:g} {reason}")


@dataclass
class Analysis:
    score: float
    strategy: str
    reasons: list[str]
    scores: dict[str, float]
    metrics: dict[str, float]

    @property
    def summary(self) -> str:
        return f"{self.strategy} {self.score:.0f}/100: " + "; ".join(self.reasons[:6])


@dataclass
class ExitSignal:
    exit: bool
    reason: str = ""


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def chart_metrics(candles: list[Candle]) -> dict[str, float]:
    if not candles:
        return {}
    c = [x.close for x in candles]
    h = [x.high for x in candles]
    lo = [x.low for x in candles]
    v = [x.volume for x in candles]
    e9, e21, e50 = ema(c, 9), ema(c, 21), ema(c, 50)
    r = rsi(c, 14)
    vw = vwap(h, lo, c, v)
    lower, mid, upper = bollinger(c, 20, 2)
    look = min(60, len(c))
    recent_high = max(h[-look:])
    return {
        "close": c[-1], "ema9": e9[-1], "ema21": e21[-1], "ema50": e50[-1],
        "rsi": r[-1], "rsi_min5": min(r[-5:]), "rsi_max10": max(r[-10:]), "rsi_prev": r[-2] if len(r) > 1 else r[-1],
        "vwap": vw[-1], "bb_lower": lower[-1], "bb_upper": upper[-1], "bb_mid": mid[-1],
        "vol_z": zscore(v, 20), "drawdown_from_high": pct(c[-1], recent_high),
        "n": float(len(c)),
    }


def score_early(p: PairInfo, candles: list[Candle], m: dict[str, float]) -> StrategyScore:
    s = StrategyScore("early")
    if p.age_minutes > 90:
        return s
    ratio = p.buy_sell_ratio("m5")
    if ratio >= 2:
        s.add(25, f"buy/sell {ratio:.1f}x")
    elif ratio >= 1.3:
        s.add(15, f"buy/sell {ratio:.1f}x")
    turnover = p.volume.get("m5", 0) / p.liquidity_usd if p.liquidity_usd else 0
    if turnover >= 0.25:
        s.add(20, f"5m vol {turnover:.0%} of liquidity")
    elif turnover >= 0.08:
        s.add(10, f"5m vol {turnover:.0%} of liquidity")
    ch5, ch1h = p.price_change.get("m5", 0), p.price_change.get("h1", 0)
    if 0 < ch5 <= 60 and ch1h > 0:
        s.add(20, f"+{ch5:.0f}% 5m / +{ch1h:.0f}% 1h")
    elif ch5 > 60:
        s.add(-20, f"parabolic +{ch5:.0f}% 5m (chasing)")
    tx = p.buys.get("m5", 0) + p.sells.get("m5", 0)
    if tx >= 60:
        s.add(15, f"{tx} txns/5m")
    elif tx >= 25:
        s.add(8, f"{tx} txns/5m")
    if p.has_socials:
        s.add(10, "has socials/website")
    if m and m["close"] > m["ema9"] and len(candles) >= 3 and candles[-1].close >= candles[-3].close:
        s.add(10, "rising above EMA9")
    return s


def score_momentum(p: PairInfo, candles: list[Candle], m: dict[str, float]) -> StrategyScore:
    s = StrategyScore("momentum")
    if len(candles) < 25:
        return s
    if m["close"] > m["ema9"] > m["ema21"]:
        s.add(30, "EMA9>EMA21 stack")
    elif m["ema9"] > m["ema21"]:
        s.add(15, "EMA9>EMA21")
    if 55 <= m["rsi"] <= 78:
        s.add(20, f"RSI {m['rsi']:.0f} power zone")
    elif m["rsi"] > 85:
        s.add(-20, f"RSI {m['rsi']:.0f} overbought")
    vz = sum(zscore([x.volume for x in candles[: len(candles) - i]], 20) for i in range(3)) / 3
    if vz >= 1:
        s.add(20, f"volume rising (z={vz:.1f})")
    greens = sum(1 for x in candles[-8:] if x.close > x.open)
    if greens >= 5:
        s.add(15, f"{greens}/8 green candles")
    if m["close"] > m["vwap"]:
        s.add(15, "above VWAP")
    if p.buy_sell_ratio("h1") < 0.8:
        s.add(-15, "sellers dominating 1h")
    return s


def score_dip(p: PairInfo, candles: list[Candle], m: dict[str, float]) -> StrategyScore:
    s = StrategyScore("dip")
    if len(candles) < 30:
        return s
    dd = m["drawdown_from_high"]
    if dd < -50:
        s.add(-40, f"down {dd:.0f}% from high (dumping)")
        return s
    uptrend = p.price_change.get("h6", 0) > 20 or p.price_change.get("h1", 0) > 15 or m["ema21"] > m["ema50"]
    if uptrend:
        s.add(25, "higher-timeframe uptrend")
    else:
        return s
    if -40 <= dd <= -12:
        s.add(25, f"pullback {dd:.0f}% from high")
    if m["rsi_min5"] < 35 and m["rsi"] > m["rsi_prev"]:
        s.add(20, f"RSI oversold ({m['rsi_min5']:.0f}) & turning up")
    last, prev = candles[-1], candles[-2]
    body = abs(last.close - last.open)
    wick = min(last.open, last.close) - last.low
    if last.close > last.open and last.close > prev.close:
        s.add(10, "bullish reversal candle")
    if wick > body * 1.5 and wick > 0:
        s.add(5, "long lower wick (buyers defending)")
    if min(x.low for x in candles[-4:]) <= m["bb_lower"] < last.close:
        s.add(15, "reclaimed lower Bollinger band")
    if p.buy_sell_ratio("m5") < 0.7:
        s.add(-25, "heavy selling right now")
    return s


def score_breakout(p: PairInfo, candles: list[Candle], m: dict[str, float]) -> StrategyScore:
    s = StrategyScore("breakout")
    if len(candles) < 25:
        return s
    last = candles[-1]
    prior = candles[-21:-1]
    range_high = max(x.high for x in prior)
    if last.close > range_high:
        s.add(35, f"closed above 20-bar high ({pct(last.close, range_high):+.1f}%)")
    else:
        return s
    avg_vol = sum(x.volume for x in prior) / len(prior)
    if avg_vol and last.volume >= 2 * avg_vol:
        s.add(25, f"volume {last.volume / avg_vol:.1f}x average")
    elif avg_vol and last.volume >= 1.3 * avg_vol:
        s.add(10, f"volume {last.volume / avg_vol:.1f}x average")
    closes = [x.close for x in candles]
    lo, mid, up = bollinger(closes[:-1], 20, 2)
    widths = [(u - l) / md for l, md, u in zip(lo[-40:], mid[-40:], up[-40:]) if md]
    if widths and widths[-1] <= sorted(widths)[len(widths) // 3]:
        s.add(15, "Bollinger squeeze before break")
    rng = last.high - last.low
    if rng and (last.close - last.low) / rng >= 0.7:
        s.add(15, "strong close near candle high")
    if m["rsi"] < 85:
        s.add(10, "not overextended")
    if p.buy_sell_ratio("m5") < 0.9:
        s.add(-15, "breakout without buy pressure")
    return s


STRATEGIES = {
    "early": score_early,
    "momentum": score_momentum,
    "dip": score_dip,
    "breakout": score_breakout,
}


def analyze(p: PairInfo, candles: list[Candle], enabled: Iterable[str]) -> Analysis:
    m = chart_metrics(candles)
    results = [STRATEGIES[name](p, candles, m) for name in enabled if name in STRATEGIES]
    if not results:
        return Analysis(0, "none", ["no strategies enabled"], {}, m)
    for r in results:
        r.score = _clamp(r.score)
    best = max(results, key=lambda r: r.score)
    return Analysis(best.score, best.name, best.reasons, {r.name: r.score for r in results}, m)


def exit_signal(p: PairInfo, candles: list[Candle], entry_price: float) -> ExitSignal:
    """Bearish chart read on an open position (stop loss / TP are handled by the risk layer)."""
    ratio5 = p.buy_sell_ratio("m5")
    sells5 = p.sells.get("m5", 0)
    if sells5 >= 20 and ratio5 < 0.4:
        return ExitSignal(True, f"dump detected: sells {sells5} vs buys {p.buys.get('m5', 0)} in 5m")
    if p.liquidity_usd and p.price_change.get("m5", 0) < -25:
        return ExitSignal(True, f"crash {p.price_change['m5']:.0f}% in 5m")
    if len(candles) < 25:
        return ExitSignal(False)
    m = chart_metrics(candles)
    closes = [x.close for x in candles]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    crossed_down = e9[-2] >= e21[-2] and e9[-1] < e21[-1]
    in_profit = m["close"] > entry_price
    if crossed_down and m["close"] < m["vwap"]:
        return ExitSignal(True, "trend break: EMA9 crossed under EMA21 below VWAP")
    if in_profit and m["rsi_max10"] > 82 and m["rsi"] < 55:
        return ExitSignal(True, f"momentum exhausted: RSI {m['rsi_max10']:.0f} -> {m['rsi']:.0f}")
    return ExitSignal(False)
