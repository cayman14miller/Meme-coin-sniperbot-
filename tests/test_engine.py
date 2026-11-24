import asyncio

from fastapi.testclient import TestClient

from sniperbot.api import create_app
from sniperbot.config import Secrets
from sniperbot.data.market import SafetyReport
from sniperbot.engine import Engine
from sniperbot.store import Store
from sniperbot.trading.broker import PaperBroker
from tests.helpers import make_pair, uptrend


class FakeMarket:
    def __init__(self):
        self.pairs = {}
        self.chart = uptrend()
        self.safe = True

    async def discover_mints(self):
        return list(self.pairs)

    async def pairs_for(self, mints):
        return {m: self.pairs[m] for m in mints if m in self.pairs}

    async def candles(self, *a, **k):
        return self.chart

    async def safety(self, mint, max_score):
        return SafetyReport(self.safe, 100, [], "" if self.safe else "danger: Mint Authority still enabled")

    async def sol_price(self):
        return 150.0

    async def close(self):
        pass


class SilentNotifier:
    channels = ["dashboard"]

    def __init__(self, store):
        self.store = store
        self.sent = []

    def send(self, level, title, message="", push=True):
        self.sent.append((level, title))
        self.store.add_event(level, title, message)

    async def telegram_commands(self, handler):
        pass

    async def close(self):
        pass


def make_engine(tmp_path):
    store = Store(str(tmp_path / "e.db"))
    market = FakeMarket()
    eng = Engine(Secrets(mode="paper"), store, market=market, broker=PaperBroker(store, 10.0),
                 notifier=SilentNotifier(store))
    eng.settings.update({"enabled": True, "trade_size": 30, "trade_unit": "USD", "use_signal_exits": False})
    return eng, market


def run(coro):
    return asyncio.run(coro)


def test_full_cycle_buy_then_stop_loss(tmp_path):
    eng, market = make_engine(tmp_path)
    p = make_pair()
    market.pairs[p.mint] = p
    run(eng.scan_once())
    open_ = eng.store.open_positions()
    assert len(open_) == 1, eng.radar
    pos = open_[0]
    assert abs(pos["cost_usd"] - 30) < 0.01
    assert eng.balance_sol < 10

    # price drops 12% -> stop loss must fire
    market.pairs[p.mint] = make_pair(price=pos["entry_price"] * 0.88)
    run(eng.monitor_once())
    assert not eng.store.open_positions()
    closed = eng.store.closed_positions()[0]
    assert "stop loss" in closed["exit_reason"]
    st = eng.store.stats()
    assert st["losses"] == 1 and st["realized_pnl_usd"] < 0


def test_unsafe_token_not_bought(tmp_path):
    eng, market = make_engine(tmp_path)
    market.safe = False
    p = make_pair()
    market.pairs[p.mint] = p
    run(eng.scan_once())
    assert not eng.store.open_positions()
    assert eng.radar[p.mint]["strategy"] == "rejected"


def test_trade_size_in_sol_and_capital_cap(tmp_path):
    eng, market = make_engine(tmp_path)
    eng.settings.update({"trade_size": 0.5, "trade_unit": "SOL", "max_capital_usd": 100})
    for i in range(3):
        mint = f"MINT{i}" + "1" * 38
        market.pairs[mint] = make_pair(mint=mint)
    run(eng.scan_once())
    pos = eng.store.open_positions()
    assert len(pos) == 1  # 0.5 SOL = $75; a second one would exceed $100 cap
    assert abs(pos[0]["cost_sol"] - 0.5) < 1e-9


def test_kill_switch_halts_and_notifies(tmp_path):
    eng, market = make_engine(tmp_path)
    eng.settings.update({"max_consecutive_losses": 2, "max_daily_loss_usd": 0, "max_drawdown_pct": 0})
    for i in range(2):
        mint = f"LOSS{i}" + "1" * 38
        market.pairs = {mint: make_pair(mint=mint)}
        run(eng.scan_once())
        entry = eng.store.open_positions()[0]["entry_price"]
        market.pairs[mint] = make_pair(mint=mint, price=entry * 0.85)
        run(eng.monitor_once())
    assert eng.risk.tripped
    assert any(level == "risk" for level, _ in eng.notifier.sent)
    # no new trades while tripped
    mint = "NEW" + "1" * 40
    market.pairs = {mint: make_pair(mint=mint)}
    run(eng.scan_once())
    assert not eng.store.open_positions()
    eng.resume_risk()
    run(eng.scan_once())
    assert len(eng.store.open_positions()) == 1


def test_api_controls(tmp_path):
    eng, market = make_engine(tmp_path)
    eng.settings.update({"enabled": False})
    app = create_app(Secrets(dashboard_token="secret", mode="paper"), engine=eng, start_engine=False)
    c = TestClient(app)
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    h = {"Authorization": "Bearer secret"}
    assert c.get("/api/state", headers=h).json()["enabled"] is False
    assert c.post("/api/toggle", json={"enabled": True}, headers=h).json()["enabled"] is True
    r = c.post("/api/settings", json={"trade_size": 0.25, "trade_unit": "SOL", "stop_loss_pct": 10}, headers=h)
    assert r.status_code == 200 and r.json()["settings"]["trade_unit"] == "SOL"
    assert c.post("/api/settings", json={"trade_unit": "EUR"}, headers=h).status_code == 400
    eng.risk.trip("test")
    eng.set_enabled(False)
    assert c.post("/api/toggle", json={"enabled": True}, headers=h).status_code == 409
    assert c.post("/api/risk/reset", json={}, headers=h).status_code == 200
    assert c.post("/api/toggle", json={"enabled": True}, headers=h).status_code == 200
    assert c.get("/").status_code == 200
