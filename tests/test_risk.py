import time

from sniperbot.config import Settings
from sniperbot.risk import RiskManager, evaluate_exit
from sniperbot.store import Store


def pos(**kw):
    p = dict(entry_price=1.0, peak_price=1.0, tp1_done=0, opened_at=time.time())
    p.update(kw)
    return p


def test_stop_loss_10pct():
    s = Settings()
    assert s.stop_loss_pct == 10
    assert evaluate_exit(pos(), 0.95, s).fraction == 0
    d = evaluate_exit(pos(), 0.899, s)
    assert d.fraction == 1 and "stop loss" in d.reason


def test_take_profit_and_trailing():
    s = Settings()
    assert evaluate_exit(pos(), 1.41, s).fraction == 0.5
    assert evaluate_exit(pos(tp1_done=1), 1.41, s).fraction == 0
    assert evaluate_exit(pos(), 2.01, s).fraction == 1
    d = evaluate_exit(pos(peak_price=1.3, tp1_done=0), 1.3 * 0.84, s)
    assert d.fraction == 1 and "trailing" in d.reason
    assert evaluate_exit(pos(tp1_done=1, peak_price=1.45), 0.999, s).fraction == 1


def test_max_hold():
    s = Settings(max_hold_minutes=10)
    assert evaluate_exit(pos(opened_at=time.time() - 3600), 1.01, s).fraction == 1


def _closed(store, pnl):
    pid = store.open_position(mint="M", symbol="M", entry_price=1, qty=1, cost_sol=0.1, cost_usd=10)
    store.update_position(pid, status="closed", closed_at=time.time(), realized_usd=10 + pnl)


def test_kill_switch_daily_loss(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    rm = RiskManager(st)
    s = Settings(max_daily_loss_usd=20, max_consecutive_losses=0, max_drawdown_pct=0)
    _closed(st, -15)
    assert rm.check(1000, s) is None
    _closed(st, -6)
    assert "daily loss" in rm.check(1000, s)


def test_kill_switch_streak_and_reset(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    rm = RiskManager(st)
    s = Settings(max_daily_loss_usd=0, max_consecutive_losses=3, max_drawdown_pct=0)
    for _ in range(3):
        _closed(st, -1)
    reason = rm.check(1000, s)
    assert "losing trades" in reason
    rm.trip(reason)
    assert rm.tripped
    time.sleep(0.01)
    rm.reset(1000)
    assert not rm.tripped
    assert rm.check(1000, s) is None  # old streak doesn't instantly re-trip


def test_kill_switch_drawdown(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    rm = RiskManager(st)
    s = Settings(max_daily_loss_usd=0, max_consecutive_losses=0, max_drawdown_pct=25)
    assert rm.check(1000, s) is None
    assert rm.check(800, s) is None
    assert "drawdown" in rm.check(740, s)


def test_settings_validation():
    s = Settings()
    assert set(s.update({"trade_size": "0.5", "trade_unit": "sol"})) == {"trade_size", "trade_unit"}
    assert s.trade_unit == "SOL"
    import pytest
    with pytest.raises(ValueError):
        s.update({"trade_unit": "EUR", "trade_size": 9})
    assert s.trade_unit == "SOL" and s.trade_size == 0.5  # rejected update changes nothing
