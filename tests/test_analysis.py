from sniperbot.analysis.indicators import ema, rsi, sma
from sniperbot.analysis.signals import analyze, exit_signal
from tests.helpers import breakout, candles_from_closes, make_pair, uptrend

ALL = ["early", "momentum", "dip", "breakout"]


def test_indicators_basic():
    assert sma([1, 2, 3, 4], 2)[-1] == 3.5
    assert ema([5] * 10, 3)[-1] == 5
    assert rsi([float(i) for i in range(1, 40)])[-1] == 100
    assert rsi([float(i) for i in range(40, 1, -1)])[-1] < 5


def test_momentum_scores_high_on_uptrend():
    a = analyze(make_pair(), uptrend(), ["momentum"])
    assert a.score >= 65, a.reasons


def test_breakout_detected():
    a = analyze(make_pair(), breakout(), ["breakout"])
    assert a.strategy == "breakout" and a.score >= 65, a.reasons


def test_dip_in_uptrend():
    closes = [1 + i * 0.02 for i in range(50)]          # run up to ~2.0
    closes += [closes[-1] * (1 - 0.03 * i) for i in range(1, 9)]  # ~24% pullback
    closes += [closes[-1] * 1.02]                         # bounce candle
    a = analyze(make_pair(), candles_from_closes(closes), ["dip"])
    assert a.score >= 55, a.reasons
    assert any("pullback" in r for r in a.reasons)


def test_flat_chart_scores_low():
    flat = candles_from_closes([1.0] * 80)
    a = analyze(make_pair(buys={"m5": 40, "h1": 300}, sells={"m5": 40, "h1": 300},
                          price_change={"m5": 0, "h1": 0, "h6": 0}), flat, ["momentum", "dip", "breakout"])
    assert a.score < 50, a.reasons


def test_early_launch():
    import time
    p = make_pair(created_at=time.time() - 15 * 60, liquidity_usd=30_000, volume={"m5": 12_000},
                  buys={"m5": 150}, sells={"m5": 50}, price_change={"m5": 12, "h1": 40})
    a = analyze(p, [], ALL)
    assert a.strategy == "early" and a.score >= 65, a.reasons


def test_parabolic_early_penalised():
    import time
    p = make_pair(created_at=time.time() - 15 * 60, price_change={"m5": 150, "h1": 300})
    a = analyze(p, [], ["early"])
    assert any("parabolic" in r for r in a.reasons)


def test_exit_on_dump():
    p = make_pair(buys={"m5": 5}, sells={"m5": 60})
    assert exit_signal(p, uptrend(), 1.0).exit


def test_v1_caps_unconfirmed_trend():
    closes = [c.close for c in uptrend()]
    flat_vol = candles_from_closes(closes, [1000] * len(closes))  # trend shape, no volume expansion
    weak = make_pair(buys={"m5": 40, "h1": 300}, sells={"m5": 40, "h1": 300})  # no buy pressure
    v0 = analyze(weak, flat_vol, ["momentum"])
    v1 = analyze(weak, flat_vol, ["momentum"], version="v1")
    assert v0.score >= 65, v0.reasons  # v0 would buy on trend indicators alone
    assert v1.score == 40 and any("capped at 40" in r for r in v1.reasons)


def test_v1_keeps_confirmed_momentum():
    a = analyze(make_pair(), uptrend(), ["momentum"], version="v1")
    assert a.score >= 65, a.reasons


def test_v1_drops_forming_candle():
    cs = uptrend()
    a = analyze(make_pair(), cs, ["momentum"], version="v1", now=cs[-1].ts + 30)
    assert a.metrics["n"] == len(cs) - 1
