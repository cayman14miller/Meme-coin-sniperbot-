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
    con.execute("INSERT INTO cycles(ts) VALUES(?)", (T0,))
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
    trades, fired, unresolved, n, _ = er.replay(db, ["all_eligible"], fetch=lambda pool, oldest: ([], "ok"))
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
