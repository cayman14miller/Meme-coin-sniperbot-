"""Failure-mode tests: the bot must never silently leave a position unprotected or untracked."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from sniperbot.api import create_app
from sniperbot.config import Secrets
from sniperbot.engine import Engine
from sniperbot.store import Store
from sniperbot.trading.broker import Broker, BrokerError, Fill, PaperBroker
from tests.helpers import make_pair
from tests.test_engine import FakeMarket, SilentNotifier


class Client:
    def __init__(self):
        self.urls = []

    async def get(self, url, **kw):
        self.urls.append(url)


class Market(FakeMarket):
    def __init__(self):
        super().__init__()
        self.jup = {}
        self.client = Client()

    async def jup_prices(self, mints):
        return {m: self.jup[m] for m in mints if m in self.jup}


class LiveLikeBroker(Broker):
    """Behaves like a live broker whose swap lands on-chain but then errors (e.g. confirmation timeout)."""
    mode = "live"

    def __init__(self, held=0.0, buy_error=True):
        self.held = held
        self.buy_error = buy_error
        self.sol = 5.0

    async def balance_sol(self):
        return self.sol

    async def buy(self, mint, sol_amount, price_usd, sol_usd, slippage_bps, priority_fee):
        if self.buy_error:
            raise BrokerError("transaction not confirmed in 60s")
        return Fill(qty=1000, sol=sol_amount, price_usd=price_usd, tx="sig")

    async def sell(self, mint, qty, price_usd, sol_usd, slippage_bps, priority_fee, sell_all=False):
        return Fill(qty=qty, sol=qty * price_usd / sol_usd, price_usd=price_usd, tx="sig")

    async def token_balance(self, mint):
        return self.held


LIVE_OK = dict(mode="live", solana_private_key="k", live_confirm=Secrets.LIVE_CONFIRM_PHRASE,
               live_max_trade_sol=1.0, live_max_deployed_sol=2.0, dashboard_token="x" * 20)


def engine(tmp_path, broker=None, **secrets):
    store = Store(str(tmp_path / "r.db"))
    market = Market()
    eng = Engine(Secrets(**({"mode": "paper"} | secrets)), store, market=market,
                 broker=broker or PaperBroker(store, 10.0), notifier=SilentNotifier(store))
    eng.settings.update({"enabled": True, "use_signal_exits": False, "trade_size": 30})
    return eng, market


def run(c):
    return asyncio.run(c)


def test_live_mode_refuses_to_start_without_interlocks(tmp_path):
    with pytest.raises(SystemExit) as e:
        Engine(Secrets(mode="live", solana_private_key="k"), Store(str(tmp_path / "x.db")),
               market=Market(), notifier=SilentNotifier(Store(str(tmp_path / "y.db"))))
    msg = str(e.value)
    assert "LIVE_TRADING_CONFIRM" in msg and "LIVE_MAX_TRADE_SOL" in msg and "DASHBOARD_TOKEN" in msg


def test_uncertain_live_buy_that_landed_is_adopted_and_protected(tmp_path):
    eng, market = engine(tmp_path, broker=LiveLikeBroker(held=1234.0), **LIVE_OK)
    p = make_pair()
    market.pairs[p.mint] = p
    run(eng.scan_once())
    pos = eng.store.open_positions()
    assert len(pos) == 1 and pos[0]["qty"] == 1234.0
    assert any("Recovered" in title for _, title in eng.notifier.sent)
    # and it is protected: a 15% drop triggers the stop loss
    market.pairs[p.mint] = make_pair(price=pos[0]["entry_price"] * 0.85)
    run(eng.monitor_once())
    assert not eng.store.open_positions()


def test_uncertain_live_buy_that_did_not_land_is_marked_failed(tmp_path):
    eng, market = engine(tmp_path, broker=LiveLikeBroker(held=0.0), **LIVE_OK)
    p = make_pair()
    market.pairs[p.mint] = p
    run(eng.scan_once())
    assert not eng.store.open_positions() and not eng.store.unresolved_positions()
    assert eng.store.stats()["trades"] == 0  # failed buys are not counted as trades


def test_paper_buy_error_leaves_balance_untouched(tmp_path):
    eng, market = engine(tmp_path)
    eng.settings.update({"trade_size": 5000})  # far beyond paper balance -> broker error
    eng.settings.update({"max_capital_usd": 10000})
    p = make_pair()
    market.pairs[p.mint] = p
    run(eng.scan_once())
    assert run(eng.broker.balance_sol()) == 10.0
    assert not eng.store.open_positions() and not eng.store.unresolved_positions()


def test_crash_mid_buy_is_reconciled_on_startup(tmp_path):
    eng, market = engine(tmp_path, broker=LiveLikeBroker(held=50.0), **LIVE_OK)
    p = make_pair()
    market.pairs[p.mint] = p
    eng.store.open_position(mint=p.mint, symbol="TEST", pair_address=p.pair_address, entry_price=p.price_usd,
                            qty=0.0, initial_qty=0.0, cost_sol=0.2, cost_usd=30, status="pending")

    async def go():
        await eng.start()
        await eng.stop()
    run(go())
    pos = eng.store.open_positions()
    assert len(pos) == 1 and pos[0]["qty"] == 50.0


def test_missing_dexscreener_price_falls_back_to_jupiter(tmp_path):
    eng, market = engine(tmp_path)
    p = make_pair()
    market.pairs[p.mint] = p
    run(eng.scan_once())
    entry = eng.store.open_positions()[0]["entry_price"]
    market.pairs.clear()                 # DexScreener lost the pair
    market.jup[p.mint] = entry * 0.85    # but Jupiter still prices it
    run(eng.monitor_once())
    closed = eng.store.closed_positions()
    assert closed and "stop loss" in closed[0]["exit_reason"] and "fallback" in closed[0]["exit_reason"]
    assert eng.store.ticks(closed[0]["id"])[-1]["source"] == "jupiter"


def test_no_price_at_all_raises_a_risk_alert(tmp_path):
    eng, market = engine(tmp_path)
    p = make_pair()
    market.pairs[p.mint] = p
    run(eng.scan_once())
    pid = eng.store.open_positions()[0]["id"]
    market.pairs.clear()
    eng._last_priced[pid] = time.time() - 120  # unpriced for 2 minutes
    run(eng.monitor_once())
    assert any(level == "risk" and "NO PRICE" in title for level, title in eng.notifier.sent)


def test_live_hard_cap_blocks_oversized_trade(tmp_path):
    eng, market = engine(tmp_path, broker=LiveLikeBroker(buy_error=False), **LIVE_OK)
    eng.settings.update({"trade_size": 2.5, "trade_unit": "SOL", "max_capital_usd": 10_000})  # > 1 SOL cap
    p = make_pair()
    market.pairs[p.mint] = p
    run(eng.scan_once())
    assert not eng.store.open_positions()
    assert any("hard cap" in title for _, title in eng.notifier.sent)


def test_sol_price_crash_does_not_trip_drawdown(tmp_path):
    eng, market = engine(tmp_path)
    eng.settings.update({"max_drawdown_pct": 25, "max_daily_loss_usd": 0, "max_consecutive_losses": 0})
    run(eng.monitor_once())
    market_price = 150.0

    async def cheaper_sol():
        return market_price / 2
    market.sol_price = cheaper_sol  # SOL halves; our SOL holdings are unchanged
    eng.market._sol = None
    run(eng.monitor_once())
    assert not eng.risk.tripped


def test_dashboard_brute_force_is_rate_limited(tmp_path):
    eng, _ = engine(tmp_path)
    c = TestClient(create_app(Secrets(dashboard_token="right-token-123456"), engine=eng, start_engine=False))
    codes = [c.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    # even the right token is refused during the lockout
    assert c.get("/api/state", headers={"Authorization": "Bearer right-token-123456"}).status_code == 429


def test_decisions_and_ticks_are_logged(tmp_path):
    eng, market = engine(tmp_path, heartbeat_url="https://hc.example/ping")
    p = make_pair()
    market.pairs[p.mint] = p
    run(eng.scan_once())
    run(eng.monitor_once())
    d = eng.store.decisions()
    assert d and d[0]["action"] == "buy" and d[0]["score"] > 0 and '"liq"' in d[0]["pair"]
    pid = eng.store.open_positions()[0]["id"]
    assert eng.store.ticks(pid)[0]["source"] == "dexscreener"
    assert market.client.urls == ["https://hc.example/ping"]
