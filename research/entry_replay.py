"""Entry-rule replay on recorder data - "what if the bot had used rule X on these coins?" (read-only).

    python -m research.entry_replay market-data.db [--rules v0,all_eligible] [--size 25]

Emulates the bot without look-ahead:
  * candidate moments = every DexScreener snapshot where the coin passed v0's static filters
    (liquidity, 5m volume, age, 5m buy/sell, FDV) - roughly every 30s, like the bot's scans
  * a rule sees only data stamped at or before that moment: the snapshot, GeckoTerminal / Jupiter / RugCheck
    rows already recorded, and 1-minute candles completed before it plus an approximated forming candle
    (open = last close, close = snapshot price, volume = elapsed share of the recent average) - the same
    partial candle the live bot sees
  * first time a rule fires -> entry at the snapshot price, no re-entry for 2h (as the bot)
  * exits = v0 rules on the recorded polled price path; a gap through the stop fills at the polled price
  * costs = pool fee per side by DEX + price impact vs the SOL side of the pool at the trade size
    (calibrated on live Jupiter quotes, research/cost_probe.py) + network fees
Portfolio limits (max open positions, capital) are ignored: this measures per-trade quality.
Results are split into chronological halves so a rule designed on the first half can be validated on the second.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from statistics import mean, median

sys.path.insert(0, os.getcwd())
from research.baseline_study import fetch_candles  # noqa: E402  (cached GeckoTerminal 1m candles)
from research.universe_study import MIN_TRADEABLE_LIQ, V0, latest_before, load, realizable, v0_pass  # noqa: E402
from sniperbot.analysis.signals import analyze  # noqa: E402
from sniperbot.data.market import Candle, PairInfo  # noqa: E402
from sniperbot.trading.broker import DEFAULT_POOL_FEE, POOL_FEE  # noqa: E402

REENTRY_SEC = 2 * 3600
NETWORK_FEE_FRAC = 0.002  # ~2 x (5000 + 100k lamports) on a $25 trade at ~$70/SOL - charged on every trade


# --------------------------------------------------------------------------- context
def pair_from_snap(s: dict, tok: dict) -> PairInfo:
    return PairInfo(
        mint=tok["mint"], symbol=tok.get("symbol") or "?", name=tok.get("name") or "", pair_address=s["pair_address"] or "",
        dex=s.get("dex") or "", price_usd=s["price_usd"], price_native=0.0, liquidity_usd=s["liq_usd"] or 0.0,
        fdv=s["fdv"] or 0.0, market_cap=s["fdv"] or 0.0, created_at=tok.get("pair_created_at") or 0.0,
        volume={"m5": s["vol_m5"] or 0.0, "h1": s["vol_h1"] or 0.0},
        price_change={"m5": s["chg_m5"] or 0.0, "h1": s["chg_h1"] or 0.0, "h6": s["chg_h6"] or 0.0},
        buys={"m5": s["buys_m5"] or 0, "h1": s["buys_h1"] or 0}, sells={"m5": s["sells_m5"] or 0, "h1": s["sells_h1"] or 0},
        boosted=bool(s["boost_active"]), has_socials=bool(s["has_socials"]))


def candles_at(raw: list[tuple], t: float, price: float, n: int = 120, forming: bool = True) -> list[Candle]:
    done = [c for c in raw if c[0] + 60 <= t][-n:]
    out = [Candle(int(c[0]), c[1], c[2], c[3], c[4], c[5]) for c in done]
    if forming and done:
        start = (int(t) // 60) * 60
        frac = (t - start) / 60
        avg_v = mean(c[5] for c in done[-5:])
        last = done[-1][4]
        out.append(Candle(start, last, max(last, price), min(last, price), price, avg_v * frac))
    return out


# --------------------------------------------------------------------------- rules
def rule_all_eligible(ctx) -> tuple[bool, float, str, list[str]]:
    return True, 0.0, "eligible", []


def rule_v0(ctx) -> tuple[bool, float, str, list[str]]:
    sf = ctx["safety"]
    if sf is None or not sf["ok"] or (sf["score"] or 0) > 5000:
        return False, 0.0, "rejected", ["safety"]
    a = analyze(ctx["pair"], ctx["candles"], ["early", "momentum", "dip", "breakout"])
    return a.score >= 65, a.score, a.strategy, a.reasons


def rule_v1_scoring(ctx) -> tuple[bool, float, str, list[str]]:
    """H8: v0 with completed candles only + volume/buy-pressure confirmation."""
    sf = ctx["safety"]
    if sf is None or not sf["ok"] or (sf["score"] or 0) > 5000:
        return False, 0.0, "rejected", ["safety"]
    completed = [c for c in ctx["candles"] if c.ts + 60 <= ctx["snap"]["ts"]]
    a = analyze(ctx["pair"], completed, ["early", "momentum", "dip", "breakout"], version="v1", now=ctx["snap"]["ts"])
    return a.score >= 65, a.score, a.strategy, a.reasons


RULES = {"v0": rule_v0, "all_eligible": rule_all_eligible, "v1_scoring": rule_v1_scoring}


# --------------------------------------------------------------------------- exits & costs
def trade_cost(dex: str, liq: float, size_usd: float) -> float:
    fee = POOL_FEE.get(dex or "", DEFAULT_POOL_FEE)
    impact = size_usd / (liq / 2) if liq else 0.05
    return 2 * (fee + impact) + NETWORK_FEE_FRAC


def exit_v0(p0: float, t0: float, after: list[tuple[float, float]], dead: bool, close_at_end: bool = False):
    """Returns (gross return, reason); None if unresolved at end of data (unless close_at_end: mark at last price)."""
    qty, realized, peak, tp1 = 1.0, 0.0, p0, False
    for item in after:
        ts, px = item[0], item[1]
        lq = item[2] if len(item) > 2 else None
        if lq is not None and lq < MIN_TRADEABLE_LIQ:  # pool drained: only its SOL side can be recovered
            return realized + qty * realizable(px / p0, lq) - 1, "liquidity collapsed"
        peak = max(peak, px)
        ch = px / p0 - 1
        if ch <= -0.10:
            return realized + qty * px / p0 - 1, "stop"
        if ch >= 1.0:
            return realized + qty * px / p0 - 1, "tp2"
        if not tp1 and ch >= 0.40:
            realized += 0.5 * px / p0
            qty, tp1 = 0.5, True
        if peak / p0 - 1 >= 0.15 and px / peak - 1 <= -0.15:
            return realized + qty * px / p0 - 1, "trail"
        if tp1 and px <= p0:
            return realized + qty * px / p0 - 1, "breakeven"
        if ts - t0 >= 240 * 60:
            return realized + qty * px / p0 - 1, "max hold"
    if dead and after:
        last = after[-1]
        return realized + qty * realizable(last[1] / p0, last[2] if len(last) > 2 else None) - 1, "dead"
    if close_at_end:
        return realized + qty * (after[-1][1] if after else p0) / p0 - 1, "end of data"
    return None


# --------------------------------------------------------------------------- replay
def replay(db: str, rule_names: list[str], size_usd: float = 25.0, fetch=fetch_candles, min_followup_min: float = 60):
    """Entries need >= min_followup_min of data after them (else skipped as 'too recent'); trades still open
    after that are marked at the last price. Counting only early-resolving trades would bias toward fast
    outcomes (quick TP2s and stops)."""
    tokens, dex, gecko, safety, sources, span, jup = load(db)
    data_end = span[1] or 0
    trades = {r: [] for r in rule_names}
    fired = {r: 0 for r in rule_names}
    unresolved = {r: 0 for r in rule_names}
    eligible_tokens = 0
    for mint, tok in tokens.items():
        path = [r for r in dex.get(mint, []) if r["price_usd"]]
        created = tok.get("pair_created_at")
        elig = [i for i, s in enumerate(path) if v0_pass(s, created)]
        if not elig:
            continue
        eligible_tokens += 1
        raw = None
        last_entry = {r: -1e18 for r in rule_names}
        for i in elig:
            s = path[i]
            t = s["ts"]
            for rn in rule_names:
                if t - last_entry[rn] < REENTRY_SEC:
                    continue
                if rn != "all_eligible" and raw is None:
                    raw, _ = fetch(s["pair_address"], path[elig[0]]["ts"] - 125 * 60)
                ctx = {"snap": s, "tok": tok, "pair": pair_from_snap(s, tok),
                       "candles": candles_at(raw or [], t, s["price_usd"]),
                       "gecko": latest_before(gecko.get(mint, []), t, 300),
                       "safety": latest_before(safety.get(mint, []), t)}
                ok, score, strat, reasons = RULES[rn](ctx)
                if not ok:
                    continue
                fired[rn] += 1
                last_entry[rn] = t
                if t > data_end - min_followup_min * 60 and not tok.get("dead_at"):
                    unresolved[rn] += 1  # too recent to judge
                    continue
                after = [(r["ts"], r["price_usd"], r["liq_usd"]) for r in path[i + 1:]]
                res = exit_v0(s["price_usd"], t, after, bool(tok.get("dead_at")), close_at_end=True)
                if res is None:
                    unresolved[rn] += 1
                    continue
                gross, why = res
                cost = trade_cost(s.get("dex") or "", s["liq_usd"] or 0, size_usd)
                trades[rn].append(dict(mint=mint, symbol=tok.get("symbol"), t=t, strategy=strat, score=score,
                                       gross=gross, net=gross - cost, cost=cost, exit=why, reasons=reasons[:4]))
    return trades, fired, unresolved, eligible_tokens, span


def ci(xs, n=4000, seed=7):
    if len(xs) < 2:
        return float("nan"), float("nan")
    rnd = random.Random(seed)
    ms = sorted(mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return ms[int(0.05 * n)], ms[int(0.95 * n) - 1]


def summarize(name: str, ts: list[dict]) -> None:
    if not ts:
        print(f"  {name:22} no resolved trades")
        return
    net = [x["net"] for x in ts]
    wins = [x for x in net if x > 0]
    loss = [x for x in net if x <= 0]
    pf = sum(wins) / -sum(loss) if loss and sum(loss) < 0 else float("inf")
    lo, hi = ci(net)
    print(f"  {name:22} n={len(ts):4} win {len(wins) / len(ts):4.0%} exp {mean(net):+7.2%} med {median(net):+7.2%} "
          f"PF {pf:5.2f} avg cost {mean(x['cost'] for x in ts):5.2%}  90% CI [{lo:+.2%}, {hi:+.2%}]")


def main(db: str, rules: list[str], size: float, half: int | None = None) -> None:
    trades, fired, unresolved, n_tok, span = replay(db, rules, size)
    hours = (span[1] - span[0]) / 3600 if span[0] else 0
    print(f"ENTRY REPLAY - {db} - {hours:.1f}h of data, {n_tok} tokens ever passed v0 filters, ${size:g}/trade")
    if half:
        mid = (span[0] + span[1]) / 2
        trades = {rn: [x for x in v if (x["t"] < mid) == (half == 1)] for rn, v in trades.items()}
        print(f"!! Restricted to entries in chronological HALF {half} of the data (pre-registered protocol).")
    for rn in rules:
        ts = sorted(trades[rn], key=lambda x: x["t"])
        print(f"\n=== rule {rn}: fired {fired[rn]} times, {len(ts)} with >=60m follow-up, "
              f"{unresolved[rn]} too recent to judge ===")
        summarize("all", ts)
        if len(ts) >= 4 and not half:
            mid = ts[len(ts) // 2]["t"]
            summarize("first half (design)", [x for x in ts if x["t"] < mid])
            summarize("second half (test)", [x for x in ts if x["t"] >= mid])
        by = defaultdict(list)
        for x in ts:
            by[x["strategy"]].append(x)
        for k, v in sorted(by.items()):
            summarize(f"strategy {k}", v)
        ex = defaultdict(int)
        for x in ts:
            ex[x["exit"]] += 1
        print("  exits: " + ", ".join(f"{k} {v}" for k, v in sorted(ex.items(), key=lambda kv: -kv[1])))
    print("\nNo look-ahead: rules see only data stamped before each moment. Portfolio limits not modelled.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(a.lstrip("-").split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    main(args[0] if args else "market-data.db", opts.get("rules", "v0,all_eligible").split(","),
         float(opts.get("size", 25)), int(opts["half"]) if "half" in opts else None)
