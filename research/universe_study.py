"""Universe study - base rates and forward outcomes for EVERY recorded candidate (read-only).

    python research/universe_study.py market-data.db

Unit of analysis = a "decision point" per token:
  * first_seen : the token's first DexScreener snapshot after discovery (the whole universe)
  * v0_eligible: the first snapshot at which the token passed the v0 bot's static entry filters
                 (liquidity, 5m volume, pair age, 5m buy/sell ratio, FDV) - what v0 would have researched
Features are taken ONLY from data stamped at or before the decision time t0 (latest DexScreener snapshot,
latest GeckoTerminal snapshot within 5 minutes, latest RugCheck report). Outcomes use ONLY snapshots
strictly after t0. A token whose liquidity collapsed and was retired is carried at its last price (you
could not have sold it).

Outcome definitions (price path from snapshots every ~30s while young, ~2 min later):
  ret_H      : return at horizon H minutes (first snapshot at or after t0+H)
  max_60/min_60 : best / worst price within 60 minutes
  dump       : min_60 <= -50%
  runner     : max_60 >= +50%
  v0_exit    : replay of v0 exits on the snapshot path (10% stop, TP1 +40% half, TP2 +100%, trail 15/15,
               240m max) with 2% round-trip cost; stop fills at the first snapshot below the stop
               (realistic for a bot polling prices, including gaps)
Nothing here is a trading signal by itself; buckets with small n are flagged.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from statistics import mean, median

V0 = dict(min_liq=15000, max_liq=5_000_000, min_vol5=2000, min_age_min=3, max_age_h=72, min_bs=1.1, max_fdv=50e6)
COST = 0.02
HORIZONS = (5, 15, 60, 120)
MIN_BUCKET = 15


def load(db: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tokens = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM tokens")}
    dex = defaultdict(list)
    for r in con.execute("SELECT ts, mint, price_usd, liq_usd, fdv, vol_m5, vol_h1, chg_m5, chg_h1, chg_h6, "
                         "buys_m5, sells_m5, buys_h1, sells_h1, boost_active, has_socials, pair_address, dex "
                         "FROM dex_snaps ORDER BY ts"):
        dex[r["mint"]].append(dict(r))
    gecko = defaultdict(list)
    for r in con.execute("SELECT ts, mint, buys_m5, sells_m5, buyers_m5, sellers_m5, traders_m5, "
                         "traders_h1, buys_h1, sells_h1, vol_m5, locked_liq_pct FROM gecko_snaps ORDER BY ts"):
        gecko[r["mint"]].append(dict(r))
    jup = defaultdict(list)
    has_jup = con.execute("SELECT 1 FROM sqlite_master WHERE name='jup_snaps'").fetchone()
    if has_jup:
        for r in con.execute("SELECT ts, mint, organic_score, organic_label, dev_mints, top_holders_pct, holders, "
                             "m5_holder_chg, m5_net_buyers, m5_org_buyers, m5_liq_chg, m5_buy_vol, m5_buy_org_vol "
                             "FROM jup_snaps ORDER BY ts"):
            jup[r["mint"]].append(dict(r))
    safety = defaultdict(list)
    for r in con.execute("SELECT ts, mint, ok, score, lp_locked_pct, danger FROM safety ORDER BY ts"):
        safety[r["mint"]].append(dict(r))
    sources = defaultdict(set)
    for m, s in con.execute("SELECT mint, source FROM sightings"):
        sources[m].add(s)
    span = con.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM cycles").fetchone()
    con.close()
    return tokens, dex, gecko, safety, sources, span, jup


def latest_before(rows, t0, max_age=None):
    ts = [r["ts"] for r in rows]
    i = bisect_right(ts, t0) - 1
    if i < 0 or (max_age is not None and t0 - rows[i]["ts"] > max_age):
        return None
    return rows[i]


def v0_pass(s: dict, created: float | None) -> bool:
    if not s["price_usd"] or not s["liq_usd"]:
        return False
    age_min = (s["ts"] - created) / 60 if created else 1e9
    bs = (s["buys_m5"] or 0) / s["sells_m5"] if s["sells_m5"] else float(s["buys_m5"] or 0)
    return (V0["min_liq"] <= s["liq_usd"] <= V0["max_liq"] and (s["vol_m5"] or 0) >= V0["min_vol5"]
            and V0["min_age_min"] <= age_min <= V0["max_age_h"] * 60 and bs >= V0["min_bs"]
            and not (s["fdv"] and s["fdv"] > V0["max_fdv"]))


MIN_TRADEABLE_LIQ = 2000.0  # below this, DexScreener prices come from drained pools and cannot be sold into
POSITION_USD = 25.0


def realizable(ratio: float, liq: float | None) -> float:
    """Value of a $POSITION_USD position as a multiple of cost, given price ratio and pool liquidity.
    Selling into a pool can never return more than its SOL side (~half the USD liquidity), and below
    MIN_TRADEABLE_LIQ we never credit a gain."""
    if liq is None or liq >= MIN_TRADEABLE_LIQ:
        return ratio
    # conservative: a drained pool never yields a profit (prints there are usually phantom)
    return min(ratio, max(liq, 0.0) / 2 / POSITION_USD, 1.0)


def outcomes(path: list[dict], i0: int, dead_at: float | None) -> dict:
    p0, t0 = path[i0]["price_usd"], path[i0]["ts"]
    after = [(r["ts"], r["price_usd"], r.get("liq_usd")) for r in path[i0 + 1:] if r["price_usd"]]
    out: dict = {"covered_min": (after[-1][0] - t0) / 60 if after else 0.0}
    last = after[-1] if after else (t0, p0, None)
    for h in HORIZONS:
        j = bisect_left([a[0] for a in after], t0 + h * 60)
        if j < len(after):
            out[f"ret_{h}"] = realizable(after[j][1] / p0, after[j][2]) - 1
        elif dead_at and dead_at <= t0 + h * 60:
            out[f"ret_{h}"] = realizable(last[1] / p0, last[2]) - 1  # retired as dead: stuck at last value
    w60 = [(px, lq) for ts, px, lq in after if ts <= t0 + 3600]
    if w60 and (out["covered_min"] >= 55 or dead_at):
        tradeable_hi = [px for px, lq in w60 if lq is None or lq >= MIN_TRADEABLE_LIQ]
        out["max_60"] = (max(tradeable_hi) / p0 - 1) if tradeable_hi else -1.0  # phantom spikes in drained pools ignored
        out["min_60"] = min(realizable(px / p0, lq) for px, lq in w60) - 1   # a drained pool counts as a dump
        out["dump"] = out["min_60"] <= -0.5
        out["runner"] = out["max_60"] >= 0.5
    out["v0_exit"] = replay_v0(p0, t0, after, dead_at)
    return out


def replay_v0(p0, t0, after, dead_at):
    """v0 exit rules on the polled path [(ts, price, liquidity)]; None if unresolved at end of data.
    If liquidity collapses below MIN_TRADEABLE_LIQ the position is exited at its realizable value."""
    qty, realized, peak, tp1 = 1.0, 0.0, p0, False
    for item in after:
        ts, px = item[0], item[1]
        lq = item[2] if len(item) > 2 else None
        if lq is not None and lq < MIN_TRADEABLE_LIQ:
            return realized + qty * realizable(px / p0, lq) - 1 - COST
        peak = max(peak, px)
        ch = px / p0 - 1
        if ch <= -0.10:
            return realized + qty * px / p0 - 1 - COST
        if ch >= 1.0:
            return realized + qty * px / p0 - 1 - COST
        if not tp1 and ch >= 0.40:
            realized += 0.5 * px / p0
            qty, tp1 = 0.5, True
        if peak / p0 - 1 >= 0.15 and px / peak - 1 <= -0.15:
            return realized + qty * px / p0 - 1 - COST
        if tp1 and px <= p0:
            return realized + qty * px / p0 - 1 - COST
        if ts - t0 >= 240 * 60:
            return realized + qty * px / p0 - 1 - COST
    if dead_at and after:
        last = after[-1]
        return realized + qty * realizable(last[1] / p0, last[2] if len(last) > 2 else None) - 1 - COST
    return None


def features(tok, path, i0, gecko, safety, sources, jup=None):
    s = path[i0]
    t0 = s["ts"]
    created = tok.get("pair_created_at")
    f = {
        "age_h": (t0 - created) / 3600 if created else None,
        "liq": s["liq_usd"], "fdv": s["fdv"], "chg_m5": s["chg_m5"], "chg_h1": s["chg_h1"], "chg_h6": s["chg_h6"],
        "turnover_m5": (s["vol_m5"] or 0) / s["liq_usd"] if s["liq_usd"] else None,
        "bs_m5": (s["buys_m5"] or 0) / s["sells_m5"] if s["sells_m5"] else None,
        "boosted": bool(s["boost_active"]), "socials": bool(s["has_socials"]),
        "first_source": tok.get("first_source"), "n_sources": len(sources.get(tok["mint"], ())),
    }
    g = latest_before(gecko.get(tok["mint"], []), t0, max_age=300)
    if g and g["traders_m5"]:
        tx = (g["buys_m5"] or 0) + (g["sells_m5"] or 0)
        f.update(tx_per_trader_m5=tx / g["traders_m5"], vol_per_trader_m5=(g["vol_m5"] or 0) / g["traders_m5"],
                 traders_m5=g["traders_m5"],
                 buyer_seller_wallets=(g["buyers_m5"] or 0) / g["sellers_m5"] if g["sellers_m5"] else None,
                 locked_liq=g["locked_liq_pct"])
    j = latest_before((jup or {}).get(tok["mint"], []), t0, max_age=300)
    if j:
        f.update(organic_score=j["organic_score"], organic_label=j["organic_label"], dev_mints=j["dev_mints"],
                 top_holders=j["top_holders_pct"], holder_chg_m5=j["m5_holder_chg"], net_buyers_m5=j["m5_net_buyers"],
                 org_buyers_m5=j["m5_org_buyers"], liq_chg_m5=j["m5_liq_chg"],
                 organic_share=(j["m5_buy_org_vol"] / j["m5_buy_vol"])
                 if j["m5_buy_vol"] and j["m5_buy_org_vol"] is not None else None)
    sf = latest_before(safety.get(tok["mint"], []), t0 + 120)  # first report may land seconds after t0
    if sf:
        f.update(rug_ok=bool(sf["ok"]), rug_score=sf["score"], lp_locked=sf["lp_locked_pct"],
                 rug_danger=json.loads(sf["danger"] or "[]"))
    return f


def bucket_table(rows, key_fn, title):
    groups = defaultdict(list)
    for r in rows:
        k = key_fn(r)
        if k is not None:
            groups[k].append(r)
    print(f"\n  {title}")
    print(f"    {'bucket':24} {'n':>5} {'dump%':>6} {'runner%':>8} {'med15':>7} {'med60':>7} {'v0 exp':>7} {'v0 n':>5}")
    for k in sorted(groups, key=str):
        g = groups[k]
        lab = [r for r in g if "dump" in r]
        r15 = [r["ret_15"] for r in g if "ret_15" in r]
        r60 = [r["ret_60"] for r in g if "ret_60" in r]
        v0 = [r["v0_exit"] for r in g if r.get("v0_exit") is not None]
        flag = " (small)" if len(lab) < MIN_BUCKET else ""
        print(f"    {str(k)[:24]:24} {len(g):5} "
              f"{(sum(r['dump'] for r in lab) / len(lab) * 100) if lab else float('nan'):5.0f}% "
              f"{(sum(r['runner'] for r in lab) / len(lab) * 100) if lab else float('nan'):7.0f}% "
              f"{(median(r15) * 100) if r15 else float('nan'):+6.1f}% {(median(r60) * 100) if r60 else float('nan'):+6.1f}% "
              f"{(mean(v0) * 100) if v0 else float('nan'):+6.1f}% {len(v0):5}{flag}")


def cut(x, edges, fmt="{}"):
    if x is None:
        return None
    for e in edges:
        if x < e:
            return f"< {fmt.format(e)}"
    return f">= {fmt.format(edges[-1])}"


def main(db: str, half: int | None = None) -> None:
    tokens, dex, gecko, safety, sources, span, jup = load(db)
    hours = (span[1] - span[0]) / 3600 if span[0] else 0
    print(f"UNIVERSE STUDY - {db} - {len(tokens)} tokens, {span[2]} cycles over {hours:.1f}h")
    if hours < 6:
        print("!! Less than 6 hours of data: one market regime, outcomes for late discoveries are censored.")

    pts = {"first_seen": [], "v0_eligible": []}
    for mint, tok in tokens.items():
        path = [r for r in dex.get(mint, []) if r["price_usd"]]
        if len(path) < 2:
            continue
        created = tok.get("pair_created_at")
        idx = {"first_seen": 0, "v0_eligible": next((i for i, s in enumerate(path) if v0_pass(s, created)), None)}
        for kind, i0 in idx.items():
            if i0 is None or i0 >= len(path) - 1:
                continue
            row = {"mint": mint, "symbol": tok.get("symbol"), "t0": path[i0]["ts"]}
            row.update(features(tok, path, i0, gecko, safety, sources, jup))
            row.update(outcomes(path, i0, tok.get("dead_at")))
            pts[kind].append(row)

    if half:
        for kind in pts:
            ts = sorted(r["t0"] for r in pts[kind])
            if ts:
                mid = ts[len(ts) // 2]
                pts[kind] = [r for r in pts[kind] if (r["t0"] < mid) == (half == 1)]
        print(f"!! Restricted to chronological HALF {half} of decision points (pre-registered protocol).")
    for kind, rows in pts.items():
        lab = [r for r in rows if "dump" in r]
        v0 = [r["v0_exit"] for r in rows if r.get("v0_exit") is not None]
        print(f"\n=== {kind.upper()} decision points: {len(rows)} (60-minute outcome known for {len(lab)}) ===")
        if not lab:
            continue
        print(f"  dumped >=50% within 60m: {sum(r['dump'] for r in lab) / len(lab):.0%} | "
              f"ran >=50% within 60m: {sum(r['runner'] for r in lab) / len(lab):.0%} | "
              f"both: {sum(r['dump'] and r['runner'] for r in lab) / len(lab):.0%}")
        for h in HORIZONS:
            rs = [r[f"ret_{h}"] for r in rows if f"ret_{h}" in r]
            if rs:
                print(f"  return at {h:3}m: median {median(rs):+7.1%} | share positive {sum(x > 0 for x in rs) / len(rs):4.0%}"
                      f" | n={len(rs)}")
        if v0:
            wins = [x for x in v0 if x > 0]
            print(f"  v0 exits replayed on polled path: n={len(v0)} win {len(wins) / len(v0):.0%} "
                  f"expectancy {mean(v0):+.1%} median {median(v0):+.1%}")

        bucket_table(rows, lambda r: r["first_source"], "by first discovery source")
        bucket_table(rows, lambda r: "boosted" if r["boosted"] else "not boosted", "by active DexScreener boost")
        bucket_table(rows, lambda r: cut(r["age_h"], [0.5, 2, 6, 24, 72], "{}h"), "by pair age")
        bucket_table(rows, lambda r: cut(r["liq"], [15e3, 50e3, 150e3, 500e3], "${:,.0f}"), "by liquidity")
        bucket_table(rows, lambda r: cut(r["chg_h1"], [0, 25, 100, 300], "{}%"), "by 1h price change at t0 (chase)")
        bucket_table(rows, lambda r: cut(r.get("tx_per_trader_m5"), [1.5, 2.5, 4, 8], "{}"),
                     "by 5m transactions per unique trader (wash-trading signal)")
        bucket_table(rows, lambda r: cut(r.get("traders_m5"), [10, 30, 100, 300], "{}"), "by 5m unique traders")
        bucket_table(rows, lambda r: None if "rug_ok" not in r else ("rugcheck ok" if r["rug_ok"] else "rugcheck danger"),
                     "by RugCheck verdict")
        bucket_table(rows, lambda r: r.get("organic_label"), "H2 by Jupiter organic score label")
        bucket_table(rows, lambda r: cut(r.get("organic_score"), [10, 30, 60], "{}"), "H2 by Jupiter organic score")
        bucket_table(rows, lambda r: cut(r.get("organic_share"), [0.1, 0.25, 0.5], "{}"), "H2 by organic share of 5m buy volume")
        bucket_table(rows, lambda r: cut(r.get("dev_mints"), [2, 10, 50, 500], "{}"), "H3 by creator's previous launches")
        bucket_table(rows, lambda r: cut(r.get("top_holders"), [15, 30, 50], "{}%"), "H4 by top-holder share")
        bucket_table(rows, lambda r: None if r.get("holder_chg_m5") is None else
                     ("H5 participation" if (r["holder_chg_m5"] > 0 and (r.get("net_buyers_m5") or 0) > 0
                                             and (r.get("org_buyers_m5") or 0) >= 10) else "not"),
                     "H5 holders rising + net & organic buyers")
        bucket_table(rows, lambda r: cut(r.get("liq_chg_m5"), [-10, 0, 10], "{}%"), "H6 by 5m liquidity change")
    print("\nRead-only. Small buckets are flagged '(small)'; differences there are noise until n grows.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    half = next((int(a.split("=")[1]) for a in sys.argv[1:] if a.startswith("--half=")), None)
    main(args[0] if args else "market-data.db", half)
