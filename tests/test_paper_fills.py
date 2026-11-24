import asyncio
import random

import pytest

from sniperbot.data.market import SOL_MINT
from sniperbot.store import Store
from sniperbot.trading.broker import BrokerError, PaperBroker

MINT = "M" * 44
SOL_USD = 150.0


class QuoteMarket:
    """Constant-price pool: 1 SOL = 1,000,000 tokens (6 decimals), 1% fee each way."""

    def __init__(self, status="ok"):
        self.status = status

    async def jup_quote(self, inp, out, amount, slippage_bps):
        if self.status != "ok":
            return self.status, "test"
        if inp == SOL_MINT:
            return "ok", int(amount / 1e9 * 1_000_000 * 1e6 * 0.99)
        return "ok", int(amount / 1e6 / 1_000_000 * 1e9 * 0.99)

    async def token_decimals(self, mint):
        return 6


def broker(tmp_path, market=None, model="quote", fail=0.0):
    return PaperBroker(Store(str(tmp_path / "p.db")), 10.0, market=market, fill_model=model, fail_rate=fail,
                       rng=random.Random(1))


def run(c):
    return asyncio.run(c)


def test_quote_fill_round_trip(tmp_path):
    b = broker(tmp_path, QuoteMarket())
    price = SOL_USD / 1_000_000  # $ per token implied by the pool
    buy = run(b.buy(MINT, 1.0, price, SOL_USD, 300, 100_000))
    assert abs(buy.qty - 990_000) < 1 and buy.tx.startswith("paper-quote")
    sell = run(b.sell(MINT, buy.qty, price, SOL_USD, 500, 100_000))
    fee = (5000 + 100_000) / 1e9
    assert abs(sell.sol - (0.99 * 0.99 - fee)) < 1e-6
    assert abs(run(b.balance_sol()) - (10 - 1 - fee + sell.sol)) < 1e-9


def test_no_route_fails_like_live_and_keeps_balance(tmp_path):
    b = broker(tmp_path, QuoteMarket(status="no_route"))
    with pytest.raises(BrokerError, match="no route"):
        run(b.buy(MINT, 1.0, 0.00015, SOL_USD, 300, 0))
    assert run(b.balance_sol()) == 10.0


def test_quote_service_down_falls_back_to_model(tmp_path):
    b = broker(tmp_path, QuoteMarket(status="unavailable"))
    b.liquidity[MINT] = 50_000
    b.dex[MINT] = "pumpswap"
    fill = run(b.buy(MINT, 0.1, 0.00015, SOL_USD, 300, 0))
    assert fill.tx.startswith("paper-model")


def round_trip(tmp_path, dex, liq, usd):
    b = broker(tmp_path, model="model")
    b.liquidity[MINT], b.dex[MINT] = liq, dex
    sol = usd / SOL_USD
    f = run(b.buy(MINT, sol, 0.001, SOL_USD, 1000, 0))
    s = run(b.sell(MINT, f.qty, 0.001, SOL_USD, 1000, 0))
    return 1 - (s.sol + 2 * 5000 / 1e9) / sol  # exclude base network fee to compare with quotes


def test_model_matches_measured_quotes(tmp_path):
    # measured medians (cost_probe, Nov 2026): pumpswap $25 @ 15-50k liq ~1.8-2.6%; $100 @ <15k ~6-7%
    assert 0.018 <= round_trip(tmp_path, "pumpswap", 25_000, 25) <= 0.030
    assert 0.055 <= round_trip(tmp_path, "pumpswap", 8_000, 100) <= 0.08
    assert round_trip(tmp_path, "meteora", 25_000, 25) < 0.012


def test_failed_transaction_costs_fee(tmp_path):
    b = broker(tmp_path, QuoteMarket(), fail=0.999999)
    with pytest.raises(BrokerError, match="simulated transaction failure"):
        run(b.buy(MINT, 1.0, 0.00015, SOL_USD, 300, 100_000))
    assert abs(run(b.balance_sol()) - (10 - 105_000 / 1e9)) < 1e-12


def test_sell_into_drained_pool_pays_almost_nothing(tmp_path):
    b = broker(tmp_path, model="model")
    b.liquidity[MINT], b.dex[MINT] = 10, "pumpswap"   # drained pool, stale displayed price
    s = run(b.sell(MINT, 25_000, 0.001, SOL_USD, 1000, 0))  # "worth" $25 at the stale price
    assert s.sol * SOL_USD < 5
