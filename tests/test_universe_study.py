import importlib.util
import pathlib
import sqlite3

from sniperbot.recorder import SCHEMA

spec = importlib.util.spec_from_file_location(
    "universe_study", pathlib.Path(__file__).parent.parent / "research" / "universe_study.py")
us = importlib.util.module_from_spec(spec)
spec.loader.exec_module(us)

T0 = 1_800_000_000.0


def snap(ts, price, liq=50_000, vol5=5000, buys=60, sells=40, fdv=500_000):
    return dict(ts=ts, price_usd=price, liq_usd=liq, fdv=fdv, vol_m5=vol5, vol_h1=0, chg_m5=1, chg_h1=10, chg_h6=0,
                buys_m5=buys, sells_m5=sells, buys_h1=0, sells_h1=0, boost_active=0, has_socials=1, pair_address="P")


def path(prices, step=60):
    return [snap(T0 + i * step, p) for i, p in enumerate(prices)]


def test_v0_filters():
    created = T0 - 3600
    assert us.v0_pass(snap(T0, 1.0), created)
    assert not us.v0_pass(snap(T0, 1.0, liq=5000), created)            # too little liquidity
    assert not us.v0_pass(snap(T0, 1.0, buys=40, sells=60), created)   # sellers dominate
    assert not us.v0_pass(snap(T0, 1.0), T0 - 60)                      # younger than 3 minutes


def test_outcomes_dump_runner_and_horizons():
    p = path([1.0] + [1.0 + 0.02 * i for i in range(1, 31)] + [0.4] * 40)  # +60% then -60%
    o = us.outcomes(p, 0, None)
    assert o["runner"] and o["dump"]
    assert abs(o["ret_5"] - 0.10) < 1e-9 and abs(o["ret_15"] - 0.30) < 1e-9 and abs(o["ret_60"] + 0.6) < 1e-9
    assert "ret_120" not in o  # path ends before 120 minutes and coin isn't dead -> censored


def test_short_path_is_censored_unless_dead():
    p = path([1.0, 0.9, 0.05])
    assert "dump" not in us.outcomes(p, 0, None)
    o = us.outcomes(p, 0, dead_at=T0 + 300)
    assert o["dump"] and abs(o["ret_60"] + 0.95) < 1e-9  # dead coin carried at its last price


def test_v0_replay_stop_fills_at_polled_price():
    after = [(T0 + 60, 0.97), (T0 + 120, 0.60)]  # gap straight through the 10% stop
    assert abs(us.replay_v0(1.0, T0, after, None) - (-0.40 - us.COST)) < 1e-9
    after = [(T0 + 60, 1.45), (T0 + 120, 2.05)]  # TP1 half at 1.45, rest at TP2
    assert abs(us.replay_v0(1.0, T0, after, None) - (0.5 * 1.45 + 0.5 * 2.05 - 1 - us.COST)) < 1e-9
    assert us.replay_v0(1.0, T0, [(T0 + 60, 1.02)], None) is None  # unresolved


def test_features_ignore_data_after_t0():
    tok = {"mint": "M", "pair_created_at": T0 - 7200, "first_source": "gecko_new"}
    p = path([1.0, 1.1])
    gecko = {"M": [dict(ts=T0 + 30, buys_m5=900, sells_m5=900, buyers_m5=3, sellers_m5=3, traders_m5=3, traders_h1=3,
                        buys_h1=0, sells_h1=0, vol_m5=1, locked_liq_pct=0)]}
    f = us.features(tok, p, 0, gecko, {}, {})
    assert "tx_per_trader_m5" not in f  # the only Gecko snapshot is after t0
    gecko["M"][0]["ts"] = T0 - 30
    f = us.features(tok, p, 0, gecko, {}, {})
    assert f["tx_per_trader_m5"] == 600
    jup = {"M": [dict(ts=T0 - 10, organic_score=5.0, organic_label="low", dev_mints=900, top_holders_pct=40.0,
                      holders=100, m5_holder_chg=-1.0, m5_net_buyers=-3, m5_org_buyers=1, m5_liq_chg=-20.0,
                      m5_buy_vol=1000.0, m5_buy_org_vol=None)]}  # Jupiter sometimes returns null organic volume
    f = us.features(tok, p, 0, gecko, {}, {}, jup)
    assert f["dev_mints"] == 900 and f["organic_share"] is None


def test_main_runs_on_recorder_schema(tmp_path, capsys):
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO tokens(mint, symbol, first_seen, first_source, last_sighting, pair_created_at) "
                "VALUES('M','MM',?, 'dex_profile', ?, ?)", (T0, T0, T0 - 3600))
    for s in path([1.0] + [0.3] * 70):
        con.execute(f"INSERT INTO dex_snaps(ts, mint, price_usd, liq_usd, fdv, vol_m5, chg_h1, buys_m5, sells_m5, "
                    f"boost_active, has_socials) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (s["ts"], "M", s["price_usd"], s["liq_usd"], s["fdv"], s["vol_m5"], 10, 60, 40, 0, 1))
    con.execute("INSERT INTO cycles(ts) VALUES(?)", (T0,))
    con.commit()
    con.close()
    us.main(str(db))
    out = capsys.readouterr().out
    assert "dumped >=50% within 60m: 100%" in out


def test_drained_pool_spike_is_not_a_runner_and_rug_is_a_total_loss():
    p = path([1.0] * 3)
    p += [dict(snap(T0 + 180, 20000.0), liq_usd=84)]       # phantom price print in an emptied pool
    p += [dict(snap(T0 + 240 + i * 60, 1e-6), liq_usd=0) for i in range(60)]
    o = us.outcomes(p, 0, None)
    assert not o["runner"] and o["dump"]
    assert o["v0_exit"] <= -us.COST + 1e-9  # no phantom profit (was +2,000,000% before the fix)


def test_rug_to_zero_liquidity_is_a_total_loss():
    p = path([1.0] * 3) + [dict(snap(T0 + 240 + i * 60, 1e-6), liq_usd=0) for i in range(60)]
    o = us.outcomes(p, 0, None)
    assert o["dump"] and abs(o["v0_exit"] - (-1 - us.COST)) < 1e-9
