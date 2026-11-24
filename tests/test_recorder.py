import asyncio
import json
import time

import httpx

from sniperbot.data.market import SOL_MINT
from sniperbot.recorder import Config, Recorder, unz

A = "A" * 43 + "pump"
B = "B" * 43 + "pump"
POOL_A, POOL_B = "PA" + "1" * 40, "PB" + "1" * 40


def dex_pair(mint, pool, liq, price=0.001, quote=SOL_MINT):
    return {"chainId": "solana", "dexId": "pumpswap", "pairAddress": pool,
            "baseToken": {"address": mint, "symbol": mint[:3], "name": "n"},
            "quoteToken": {"address": quote, "symbol": "SOL"}, "priceUsd": str(price), "priceNative": "0.00001",
            "txns": {"m5": {"buys": 50, "sells": 40}, "h1": {"buys": 500, "sells": 400}},
            "volume": {"m5": 5000, "h1": 40000}, "priceChange": {"m5": 3, "h1": 20},
            "liquidity": {"usd": liq, "quote": liq / 2 / 150}, "fdv": 500000, "marketCap": 500000,
            "pairCreatedAt": int((time.time() - 3600) * 1000), "info": {"socials": [{"type": "twitter"}]},
            "boosts": {"active": 10}}


def gecko_pool(pool, mint, buys=90, traders=12):
    return {"id": f"solana_{pool}", "attributes": {
        "address": pool, "base_token_price_usd": "0.001", "reserve_in_usd": "50000", "fdv_usd": "500000",
        "locked_liquidity_percentage": "100",
        "transactions": {w: {"buys": buys, "sells": 40, "buyers": traders, "sellers": 8, "unique_traders": traders}
                         for w in ("m5", "m15", "m30", "h1")},
        "volume_usd": {"m5": "5000", "m15": "12000", "h1": "40000"}},
        "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}}}}


class FakeAPI:
    def __init__(self):
        self.liq = {A: 80000, B: 30000}
        self.calls: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        key = u.split("?")[0].split("/")[-2] if "/tokens/v1/" not in u else "tokens"
        self.calls[key] = self.calls.get(key, 0) + 1
        if "token-profiles" in u:
            body = [{"chainId": "solana", "tokenAddress": A}, {"chainId": "ethereum", "tokenAddress": "0xabc"}]
        elif "token-boosts/latest" in u:
            body = [{"chainId": "solana", "tokenAddress": A, "amount": 100, "totalAmount": 500}]
        elif "token-boosts/top" in u:
            body = []
        elif "new_pools" in u:
            body = {"data": [gecko_pool(POOL_B, B)]}
        elif "trending_pools" in u:
            body = {"data": []}
        elif "/tokens/v1/solana/" in u:
            mints = u.split("/tokens/v1/solana/")[1].split("?")[0].split(",")
            pools = {A: POOL_A, B: POOL_B}
            body = [dex_pair(m, pools[m], self.liq[m]) for m in mints if m in pools]
            body += [dex_pair(A, "WORSE", 100)]  # a second, smaller pair must not be chosen
        elif "/pools/multi/" in u:
            pools = u.split("/pools/multi/")[1].split("?")[0].split(",")
            back = {POOL_A: A, POOL_B: B}
            body = {"data": [gecko_pool(p, back[p], buys=300, traders=3) for p in pools if p in back]}
        elif "rugcheck" in u:
            body = {"score": 1, "score_normalised": 1, "lpLockedPct": 100,
                    "risks": [{"name": "Mutable metadata", "level": "warn"}]}
        elif "tokens/v2/search" in u:
            mints = dict(httpx.QueryParams(request.url.query))["query"].split(",")
            body = [{"id": m, "usdPrice": 0.001, "liquidity": self.liq[m], "mcap": 500000, "holderCount": 321,
                     "organicScore": 12.5, "organicScoreLabel": "low",
                     "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True,
                               "topHoldersPercentage": 41.0, "devMints": 1507, "devMigrations": 3},
                     "launchpad": "pump.fun",
                     "stats5m": {"buyVolume": 9000, "buyOrganicVolume": 300, "numTraders": 40, "numOrganicBuyers": 4,
                                 "holderChange": -2.0, "liquidityChange": -30.0},
                     "stats1h": {"priceChange": 250.0}} for m in mints if m in self.liq]
        elif "price/v3" in u:
            body = {SOL_MINT: {"usdPrice": 150.0}}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=body)


def make(tmp_path, api, **cfg):
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    c = Config(dex_per_min=100000, gecko_per_min=100000, rug_per_min=100000, **cfg)
    return Recorder(str(tmp_path / "m.db"), c, client)


def q(rec, sql):
    return rec.db.execute(sql).fetchall()


def test_cycle_records_everything(tmp_path):
    api = FakeAPI()
    rec = make(tmp_path, api)
    st = asyncio.run(rec.cycle())
    assert st.new_tokens == 2 and st.errors == 0
    assert {r[0] for r in q(rec, "SELECT mint FROM tokens")} == {A, B}
    srcs = {(m, s) for m, s in q(rec, "SELECT mint, source FROM sightings")}
    assert (A, "dex_profile") in srcs and (A, "dex_boost_latest") in srcs and (B, "gecko_new") in srcs
    assert q(rec, "SELECT boost_amount, boost_total FROM sightings WHERE source='dex_boost_latest'") == [(100, 500)]
    snaps = q(rec, "SELECT mint, pair_address, liq_usd, buys_m5, sells_m5, raw FROM dex_snaps ORDER BY mint")
    assert [(m, p) for m, p, *_ in snaps] == [(A, POOL_A), (B, POOL_B)]  # best (highest-liquidity) pair chosen
    assert unz(snaps[0][5])["pairAddress"] == POOL_A  # raw kept for first snapshot
    g = q(rec, "SELECT mint, source, buys_m5, traders_m5, locked_liq_pct FROM gecko_snaps")
    assert (B, "gecko_new", 90, 12, 100) in g  # from discovery feed
    # RugCheck/GeckoTerminal detail waits until liquidity is known (first DexScreener snapshot)
    assert q(rec, "SELECT count(*) FROM safety") == [(0,)]
    asyncio.run(rec.cycle())
    assert q(rec, "SELECT ok, score, lp_locked_pct, n_risks FROM safety") == [(1, 1, 100, 1)] * 2
    assert {r[0] for r in q(rec, "SELECT usd FROM sol_price")} == {150.0}
    tok = q(rec, f"SELECT pair_address, symbol, pair_created_at FROM tokens WHERE mint='{A}'")[0]
    assert tok[0] == POOL_A and tok[1] == "AAA" and tok[2] > 0


def test_second_cycle_gets_unique_traders_and_skips_rediscovery(tmp_path):
    api = FakeAPI()
    rec = make(tmp_path, api)
    asyncio.run(rec.cycle())
    asyncio.run(rec.cycle())  # within 60s: no rediscovery, but young coins snapshot every cycle
    assert api.calls["latest"] == 2  # profiles + boosts once each (keyed by path segment)
    assert len(q(rec, "SELECT 1 FROM dex_snaps")) == 4
    rec.tracked[A].last_gecko = rec.tracked[B].last_gecko = 0  # make the multi-pool snapshot due again
    asyncio.run(rec.cycle())
    multi = q(rec, "SELECT mint, buys_m5, traders_m5 FROM gecko_snaps WHERE source='multi'")
    assert set(multi) == {(A, 300, 3), (B, 300, 3)}  # 300 txns from 3 wallets = wash-trading signal
    assert len(q(rec, "SELECT 1 FROM safety")) == 2  # checked once, not re-checked within the hour


def test_jupiter_token_stats_capture_wash_and_dev_signals(tmp_path):
    api = FakeAPI()
    rec = make(tmp_path, api)
    asyncio.run(rec.cycle())  # liquidity learned
    asyncio.run(rec.cycle())  # jupiter stats due
    rows = q(rec, "SELECT mint, holders, organic_score, top_holders_pct, dev_mints, m5_buy_vol, m5_buy_org_vol, "
                  "m5_liq_chg, h1_price_chg, launchpad FROM jup_snaps ORDER BY mint")
    assert rows[0] == (A, 321, 12.5, 41.0, 1507, 9000, 300, -30.0, 250.0, "pump.fun")
    assert len(rows) == 2


def test_dead_coin_is_retired_and_restart_resumes(tmp_path):
    api = FakeAPI()
    rec = make(tmp_path, api, dead_after_sec=0)
    asyncio.run(rec.cycle())
    api.liq[B] = 50  # liquidity pulled
    asyncio.run(rec.cycle())
    assert B not in rec.tracked and A in rec.tracked
    assert q(rec, f"SELECT dead_at IS NOT NULL FROM tokens WHERE mint='{B}'") == [(1,)]
    rec.db.close()
    rec2 = make(tmp_path, api)
    assert set(rec2.tracked) == {A}  # resumes only live coins inside the tracking window


def test_untradeable_and_illiquid_coins_skip_detail_and_retire(tmp_path):
    api = FakeAPI()
    api.liq[B] = 3000  # below the $5k detail threshold but above "dead"
    rec = make(tmp_path, api, dead_after_sec=0)
    asyncio.run(rec.cycle())
    asyncio.run(rec.cycle())
    assert q(rec, f"SELECT count(*) FROM safety WHERE mint='{B}'") == [(0,)]
    assert q(rec, f"SELECT count(*) FROM gecko_snaps WHERE mint='{B}' AND source='multi'") == [(0,)]
    assert B in rec.tracked  # still followed for its price path


def test_api_errors_are_counted_not_fatal(tmp_path):
    def broken(request):
        return httpx.Response(500)
    rec = make(tmp_path, broken)
    st = asyncio.run(rec.cycle())
    assert st.errors > 0 and st.new_tokens == 0
    assert q(rec, "SELECT errors > 0 FROM cycles") == [(1,)]
