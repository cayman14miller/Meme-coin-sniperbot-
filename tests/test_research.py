"""Replay-engine checks for research/baseline_study.py (no network)."""
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "baseline_study", pathlib.Path(__file__).parent.parent / "research" / "baseline_study.py")
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)

T0 = 1_700_000_000 // 60 * 60


def candles(rows):
    """rows: (minute, open, high, low, close)"""
    return [(T0 + m * 60, o, h, l, c, 100.0) for m, o, h, l, c in rows]


def test_rug_candle_pessimistic_fill():
    c = candles([(0, 1, 1, 1, 1), (1, 1.0, 1.02, 0.12, 0.14), (2, .14, .14, .14, .14)])
    opt = bs.simulate(c, 1.0, T0 + 30, {}, None, pessimistic=False)
    pes = bs.simulate(c, 1.0, T0 + 30, {}, None, pessimistic=True)
    assert abs(opt["ret"] - (-0.10 - bs.COST)) < 1e-9      # optimistic: filled at the stop
    assert abs(pes["ret"] - (-0.86 - bs.COST)) < 1e-9      # pessimistic: filled at crash close


def test_gap_open_fills_at_open():
    c = candles([(0, 1, 1, 1, 1), (1, 0.7, 0.75, 0.6, 0.65)])
    r = bs.simulate(c, 1.0, T0 + 30, {}, None, pessimistic=False)
    assert r["reason"] == "stop gap" and abs(r["ret"] - (-0.30 - bs.COST)) < 1e-9


def test_entry_minute_uses_only_close():
    # the entry candle's low (0.5) happened before our fill and must not trigger the stop
    c = candles([(0, 1, 1.0, 0.5, 1.0), (1, 1, 1.05, 0.99, 1.0)])
    r = bs.simulate(c, 1.0, T0 + 30, {}, None, pessimistic=True)
    assert r["reason"] == "open at data end"


def test_take_profit_then_trailing_stop():
    c = candles([(0, 1, 1, 1, 1), (1, 1, 1.45, 1, 1.4), (2, 1.4, 1.4, 0.99, 1.0)])
    r = bs.simulate(c, 1.0, T0 + 30, {}, None, pessimistic=False)
    # half sold at +40%; peak 1.45 -> 15% trail at 1.2325 sits above break-even and fires first
    assert abs(r["ret"] - (0.5 * 1.4 + 0.5 * 1.45 * 0.85 - 1 - bs.COST)) < 1e-9


def test_break_even_after_tp1():
    c = candles([(0, 1, 1, 1, 1), (1, 1, 1.45, 1, 1.4), (2, 1.4, 1.4, 0.99, 1.0)])
    r = bs.simulate(c, 1.0, T0 + 30, {"act": 5.0}, None, pessimistic=False)  # trailing disabled
    assert abs(r["ret"] - (0.5 * 1.4 + 0.5 * 1.0 - 1 - bs.COST)) < 1e-9


def test_time_stop():
    rows = [(0, 1, 1, 1, 1)] + [(m, 1, 1.01, 0.99, 1.0) for m in range(1, 15)]
    r = bs.simulate(candles(rows), 1.0, T0 + 30, {"time_stop": 10, "time_min_gain": 0.03}, None, True)
    assert r["reason"] == "time stop"


def test_entry_features_no_lookahead():
    rows = [(m, 1, 1, 1, 1) for m in range(-30, 0)] + [(0, 1, 5, 1, 5), (1, 5, 9, 5, 9)]
    f = bs.entry_features(candles(rows), T0 + 30, 1.0)
    assert f["n_prior"] == 30 and abs(f["from_high15"]) < 1e-9  # future spike to 9 not visible
