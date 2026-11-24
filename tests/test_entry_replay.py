import sqlite3

from research import entry_replay as er
from sniperbot.recorder import SCHEMA

T0 = 1_800_000_000.0


def make_db(tmp_path, prices, liq=50_000, safety_ok=1):
    db = tmp_path / "r.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO tokens(mint, symbol, first_seen, first_source, last_sighting, pair_created_at) "
                "VALUES('M','MM',?,?,?,?)", (T0, "dex_profile", T0, T0 - 3 * 3600))
    for i, p in enumerate(prices):
        con.execute("INSERT INTO dex_snaps(ts, mint, pair_address, dex, price_usd, liq_usd, fdv, vol_m5, chg_m5, chg_h1, "
                    "chg_h6, buys_m5, sells_m5, buys_h1, sells_h1, boost_active, has_socials) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (T0 + i * 30, "M", "POOL", "pumpswap", p, liq, 500_000, 5000, 1, 10, 0, 60, 40, 600, 400, 0, 1))
    con.execute("INSERT INTO safety(ts, mint, ok, score) VALUES(?,?,?,?)", (T0 - 10, "M", safety_ok, 1))
    con.executemany("INSERT INTO cycles(ts) VALUES(?)", [(T0,), (T0 + (len(prices) - 1) * 30,)])
    con.commit()
    con.close()
    return str(db)


def test_forming_candle_and_no_lookahead():
    raw = [(T0 - 120, 1, 1, 1, 1.0, 100), (T0 - 60, 1, 1, 1, 1.0, 100), (T0, 1, 9, 1, 9.0, 999)]
    cs = er.candles_at(raw, T0 + 30, 1.1)
    assert [c.close for c in cs] == [1.0, 1.0, 1.1]  # the future 9.0 candle is invisible
    assert cs[-1].volume == 50  # half a minute of average volume


def test_all_eligible_trades_once_per_cooldown_with_costs(tmp_path):
    prices = [1.0] * 4 + [0.85] * 10  # buy then stop out
    db = make_db(tmp_path, prices)
    trades, fired, unresolved, n, _ = er.replay(db, ["all_eligible"], fetch=lambda pool, oldest: ([], "ok"),
                                                min_followup_min=0)
    t = trades["all_eligible"]
    assert n == 1 and fired["all_eligible"] == 1 and len(t) == 1  # no re-entry within 2h
    assert t[0]["exit"] == "stop" and abs(t[0]["gross"] + 0.15) < 1e-9
    assert abs(t[0]["cost"] - er.trade_cost("pumpswap", 50_000, 25)) < 1e-12
    assert t[0]["net"] < t[0]["gross"]


def test_v0_rule_respects_safety(tmp_path):
    db = make_db(tmp_path, [1.0 + 0.01 * i for i in range(40)], safety_ok=0)
    trades, fired, *_ = er.replay(db, ["v0"], fetch=lambda pool, oldest: ([], "ok"))
    assert fired["v0"] == 0


def test_trade_cost_matches_measured_scale():
    assert 0.018 < er.trade_cost("pumpswap", 50_000, 25) < 0.03
    assert er.trade_cost("meteora", 50_000, 25) < 0.012


def test_recent_entries_are_not_judged_and_open_trades_marked_at_end(tmp_path):
    db = make_db(tmp_path, [1.0] * 4 + [1.05] * 200)  # 100 minutes of flat-ish data after entry
    trades, fired, unresolved, *_ = er.replay(db, ["all_eligible"], fetch=lambda pool, oldest: ([], "ok"))
    t = trades["all_eligible"]
    assert len(t) == 1 and t[0]["exit"] == "end of data" and abs(t[0]["gross"] - 0.05) < 1e-9
    (tmp_path / "x").mkdir()
    db2 = make_db(tmp_path / "x", [1.0] * 10)  # 5 minutes only
    trades, fired, unresolved, *_ = er.replay(db2, ["all_eligible"], fetch=lambda pool, oldest: ([], "ok"))
    assert not trades["all_eligible"] and unresolved["all_eligible"] == 1


def test_liquidity_collapse_exits_at_realizable_value():
    res = er.exit_v0(1.0, T0, [(T0 + 30, 1.0, 50_000), (T0 + 60, 50.0, 10)], dead=False)
    assert res[1] == "liquidity collapsed" and res[0] < -0.5  # $10 pool can't pay out a $25 position


def test_v1_filters_reject_young_pairs_chasing_and_wash():
    base = {"ts": T0, "chg_h1": 50}
    tok = {"pair_created_at": T0 - 3600}
    sf = {"ok": 1, "score": 1}
    good_g = {"buys_m5": 60, "sells_m5": 40, "traders_m5": 50}
    ok = lambda **kw: er.v1_filters_pass({**dict(snap=base, tok=tok, gecko=good_g, safety=sf), **kw})[0]
    assert ok()
    assert not ok(tok={"pair_created_at": T0 - 600})                         # H9: 10 minutes old
    assert not ok(snap={"ts": T0, "chg_h1": 250})                            # H1: chasing
    assert not ok(gecko={"buys_m5": 300, "sells_m5": 100, "traders_m5": 20})  # H2: 20 tx per trader
    assert not ok(gecko=None)                                                # missing data -> fail
    assert not ok(safety={"ok": 0, "score": 1})
