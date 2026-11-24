"""Baseline study v2 - read-only analysis of a paper-trading database.

Run from the project root:
    python research/baseline_study.py paper-run1.db

What it does (never writes to the database; only GETs public 1m candles from GeckoTerminal):
  A. Data coverage per trade.
  B. Actual paper performance: win rate, avg win/loss, expectancy, profit factor, max drawdown,
     results by strategy, bootstrap confidence interval on expectancy.
  C. Entry timing using ONLY candles completed before the entry (no look-ahead): run-up before
     entry, distance above VWAP / EMA21 / recent high, RSI, ATR, volume trend.
  D. Path after entry: maximum favourable / adverse excursion (MFE / MAE) and which came first.
  E. Path after stop-loss exits: recoveries and further declines, using candles strictly after
     the exit minute (fixes the "-1m" artifact of v1).
  F. Exit-rule replay of all trades with two fill models (optimistic: fill at stop level;
     pessimistic: fill at min(stop, candle close) - captures crash candles like a rug).

Limitations are printed with the results. Small samples are flagged; nothing here proves an edge.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from statistics import mean, median

sys.path.insert(0, os.getcwd())
try:
    from sniperbot.analysis.indicators import atr, ema, rsi, vwap
except ImportError:  # pragma: no cover
    sys.exit("Run this from the project root (the folder that contains 'sniperbot').")

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    SSL_CTX = ssl.create_default_context()

GECKO = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{}/ohlcv/minute"
CACHE_DIR = os.path.join("/tmp", "sniper_study_cache")
CACHE_MAX_AGE = 20 * 60
COST = 0.02  # round-trip fees + slippage applied to every replayed trade
DEFAULTS = dict(s=0.10, act=0.15, trail=0.15, tp1=0.40, tp1_frac=0.5, tp2=1.00, be_after_tp1=True, max_hold=240)


# --------------------------------------------------------------------------- data
def _http_json(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "baseline-study"})
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
        return json.load(r)


def fetch_candles(pool: str, oldest_needed: float, refresh: bool = False):
    """1m candles [(ts,o,h,l,c,v)] oldest->newest. Cached in /tmp; backs off on HTTP 429."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{pool}.json")
    if not refresh and os.path.exists(path):
        cached = json.load(open(path))
        if time.time() - cached["fetched_at"] < CACHE_MAX_AGE and cached["candles"] and \
                cached["candles"][0][0] <= oldest_needed:
            return [tuple(c) for c in cached["candles"]], "cache"
    out: dict[int, tuple] = {}
    before = None
    status = "ok"
    for _ in range(4):
        q = {"aggregate": 1, "limit": 1000, "currency": "usd"}
        if before:
            q["before_timestamp"] = before
        url = GECKO.format(pool) + "?" + urllib.parse.urlencode(q)
        rows = None
        for wait in (0, 15, 30, 60, 90):
            if wait:
                time.sleep(wait)
            try:
                rows = _http_json(url)["data"]["attributes"]["ohlcv_list"]
                break
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    status = f"HTTP {e.code}"
                    break
                status = "rate-limited"
            except Exception as e:  # network
                status = f"error: {e}"
        time.sleep(2.5)  # GeckoTerminal free tier ~30 req/min
        if rows is None:
            break
        status = "ok"
        if not rows:
            break
        for c in rows:
            out[int(c[0])] = (int(c[0]),) + tuple(float(x) for x in c[1:6])
        oldest = min(int(c[0]) for c in rows)
        if oldest <= oldest_needed:
            break
        before = oldest
    candles = sorted(out.values())
    if not candles and status == "ok":
        status = "no candles returned"
    if candles:
        json.dump({"fetched_at": time.time(), "candles": candles}, open(path, "w"))
    return candles, status


# --------------------------------------------------------------------------- helpers
def pct(x):
    return "   n/a" if x is None else f"{x * 100:+6.1f}%"


def stats(rets: list[float]) -> dict:
    if not rets:
        return {}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for r in rets:  # equal-size trades, sequential
        eq += r
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return dict(n=len(rets), win=len(wins) / len(rets), avg_win=mean(wins) if wins else 0.0,
                avg_loss=mean(losses) if losses else 0.0, exp=mean(rets), total=sum(rets),
                pf=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None, mdd=mdd)


def bootstrap_ci(rets: list[float], n: int = 4000, seed: int = 7) -> tuple[float, float]:
    if len(rets) < 2:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    ms = sorted(mean(rnd.choices(rets, k=len(rets))) for _ in range(n))
    return ms[int(0.05 * n)], ms[int(0.95 * n) - 1]


def entry_features(candles, entry_ts: float, entry_px: float) -> dict:
    """Features from candles COMPLETED before entry (no look-ahead)."""
    prior = [c for c in candles if c[0] + 60 <= entry_ts][-120:]
    f: dict = {"n_prior": len(prior)}
    if len(prior) < 5:
        return f
    closes = [c[4] for c in prior]
    highs = [c[2] for c in prior]
    lows = [c[3] for c in prior]
    vols = [c[5] for c in prior]

    def runup(mins):
        ref = [c for c in prior if c[0] <= entry_ts - mins * 60]
        return entry_px / ref[-1][4] - 1 if ref else None

    f.update(runup5=runup(5), runup15=runup(15), runup60=runup(60),
             from_high15=entry_px / max(highs[-15:]) - 1,
             vwap_dist=entry_px / vwap(highs, lows, closes, vols)[-1] - 1,
             ema21_dist=entry_px / ema(closes, 21)[-1] - 1,
             rsi=rsi(closes, 14)[-1],
             atr=atr(highs, lows, closes, 14)[-1] / closes[-1])
    if len(vols) >= 23:
        base = mean(vols[-23:-3]) or 1e-9
        f["vol_ratio"] = mean(vols[-3:]) / base
    return f


def path_after(candles, start_ts: float, ref_px: float, minutes: int):
    """MFE / MAE vs ref_px from start_ts for `minutes`. Start-minute candle contributes only its close."""
    w = [c for c in candles if c[0] + 60 > start_ts and c[0] <= start_ts + minutes * 60]
    if not w:
        return None
    hi_i = lo_i = 0
    hi, lo = -1e18, 1e18
    for i, c in enumerate(w):
        h, l = (c[4], c[4]) if c[0] <= start_ts else (c[2], c[3])
        if h > hi:
            hi, hi_i = h, i
        if l < lo:
            lo, lo_i = l, i
    if lo / ref_px - 1 > -0.005:
        order = "no dip"
    elif hi / ref_px - 1 < 0.005:
        order = "no rise"
    else:
        order = "low first" if lo_i < hi_i else "high first" if hi_i < lo_i else "same candle"
    return dict(mfe=hi / ref_px - 1, mae=lo / ref_px - 1, order=order, mins=(w[-1][0] - start_ts) / 60,
                t_mfe=(w[hi_i][0] - start_ts) / 60, t_mae=(w[lo_i][0] - start_ts) / 60)


# --------------------------------------------------------------------------- replay
RULES = {
    "A current: 10% stop, trail 15/15": {},
    "B fixed 15%": dict(s=0.15),
    "C fixed 20%": dict(s=0.20),
    "D fixed 25%": dict(s=0.25),
    "E ATR x3 (8-25%)": dict(atr_k=3, lo=0.08, hi=0.25),
    "F ATR x4 (10-30%)": dict(atr_k=4, lo=0.10, hi=0.30),
    "G 1m close < -10%, hard -25%": dict(close_confirm=True, hard=0.25),
    "H trail act 5 / trail 8": dict(act=0.05, trail=0.08),
    "I trail act 8 / trail 10": dict(act=0.08, trail=0.10),
    "J trail act 10 / trail 12": dict(act=0.10, trail=0.12),
    "K trail 8/10 + break-even at +10%": dict(act=0.08, trail=0.10, be_at=0.10),
    "L time stop: <+3% after 10m -> exit": dict(time_stop=10, time_min_gain=0.03),
    "M 1/3 at +20%, then break-even": dict(tp1=0.20, tp1_frac=1 / 3),
    "N trail 8/10 + time stop 10m": dict(act=0.08, trail=0.10, time_stop=10, time_min_gain=0.03),
}


def simulate(candles, entry, entry_ts, overrides, atrp, pessimistic):
    r = {**DEFAULTS, **overrides}
    s = r["s"]
    if "atr_k" in r:
        s = min(max(r["atr_k"] * (atrp if atrp else 0.05), r["lo"]), r["hi"])
    path = [c for c in candles if c[0] + 60 > entry_ts]
    if not path:
        return None
    qty, realized, peak, tp1 = 1.0, 0.0, entry, False
    exit_px, reason = path[-1][4], "open at data end"
    for i, (ts, o, h, l, c, _v) in enumerate(path):
        if ts <= entry_ts:  # entry minute: only its close is after our fill
            o = h = l = c
        t_min = (ts - entry_ts) / 60
        stop = entry * (1 - (r["hard"] if r.get("close_confirm") else s))
        if tp1 and r["be_after_tp1"]:
            stop = max(stop, entry)
        if r.get("be_at") and peak >= entry * (1 + r["be_at"]):
            stop = max(stop, entry)
        if peak >= entry * (1 + r["act"]):
            stop = max(stop, peak * (1 - r["trail"]))
        if o <= stop:
            exit_px, reason = o, "stop gap"
            break
        if l <= stop:
            exit_px, reason = (min(stop, c) if pessimistic else stop), "stop"
            break
        if r.get("close_confirm") and not tp1 and c <= entry * (1 - s):
            exit_px, reason = c, "stop close"
            break
        peak = max(peak, h)
        if not tp1 and h >= entry * (1 + r["tp1"]):
            realized += r["tp1_frac"] * entry * (1 + r["tp1"])
            qty -= r["tp1_frac"]
            tp1 = True
        if h >= entry * (1 + r["tp2"]):
            exit_px, reason = entry * (1 + r["tp2"]), "tp2"
            break
        if r.get("time_stop") and t_min >= r["time_stop"] and not tp1 and c < entry * (1 + r["time_min_gain"]) \
                and peak < entry * (1 + r["act"]):
            exit_px, reason = c, "time stop"
            break
        if t_min >= r["max_hold"]:
            exit_px, reason = c, "max hold"
            break
    ret = (realized + qty * exit_px) / entry - 1 - COST
    return dict(ret=ret, reason=reason, risk=s)


# --------------------------------------------------------------------------- report
def main(db: str, refresh: bool = False) -> None:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pos = [dict(r) for r in con.execute("SELECT * FROM positions WHERE status='closed' ORDER BY opened_at")]
    con.close()
    if not pos:
        sys.exit("No closed trades yet.")
    print(f"BASELINE STUDY v2 - {db} - {len(pos)} closed trades")
    if len(pos) < 30:
        print(f"!! {len(pos)} trades is a SMALL SAMPLE. Every number below is directional, not proof.")

    # ---- A. data
    print("\n=== A. DATA COVERAGE ===")
    data = {}
    for p in pos:
        c, st = fetch_candles(p["pair_address"], p["opened_at"] - 125 * 60, refresh)
        data[p["id"]] = c
        before = sum(1 for x in c if x[0] + 60 <= p["opened_at"])
        after_exit = (c[-1][0] - p["closed_at"]) / 60 if c else 0
        print(f"  {p['symbol'][:10]:10} candles {len(c):4}  before entry {before:4}  "
              f"after exit {after_exit:6.0f}m  [{st}]")
    usable = [p for p in pos if data[p["id"]]]

    # ---- B. actual performance
    print("\n=== B. ACTUAL PAPER PERFORMANCE (fees + simulated slippage included) ===")
    rets = [(p["realized_usd"] - p["cost_usd"]) / p["cost_usd"] for p in pos]
    usd = [p["realized_usd"] - p["cost_usd"] for p in pos]
    s = stats(rets)
    lo, hi = bootstrap_ci(rets)
    print(f"  trades {s['n']} | win rate {s['win']:.0%} | avg win {pct(s['avg_win'])} | avg loss {pct(s['avg_loss'])}")
    print(f"  expectancy {pct(s['exp'])}/trade (${mean(usd):+.2f}) | 90% bootstrap CI [{pct(lo)}, {pct(hi)}]")
    print(f"  profit factor {s['pf'] if s['pf'] is None else round(s['pf'], 2)} | total ${sum(usd):+.2f} | "
          f"max drawdown {pct(s['mdd'])} (cumulative, in units of one position)")
    print("  Sharpe: not reported - meaningless at this sample size.")
    print("  by strategy:")
    for strat in sorted({(p["strategy"] or "?").replace("manual:", "") for p in pos}):
        rs = [r for r, p in zip(rets, pos) if (p["strategy"] or "?").replace("manual:", "") == strat]
        ss = stats(rs)
        print(f"    {strat:9} n={ss['n']:2}  win {ss['win']:4.0%}  exp {pct(ss['exp'])}  total {pct(ss['total'])}")
    no_tail = [r for r in rets if r > -0.5]
    if len(no_tail) != len(rets):
        print(f"  excluding {len(rets) - len(no_tail)} catastrophic loss(es) (< -50%): expectancy "
              f"{pct(mean(no_tail))} - tail events dominate results")

    # ---- C. entry timing
    print("\n=== C. ENTRY TIMING (completed candles before entry only) ===")
    print(f"  {'token':10} {'strat':8} {'result':>7} {'run5':>7} {'run15':>7} {'run60':>7} {'vsHi15':>7} "
          f"{'vsVWAP':>7} {'vsEMA21':>7} {'RSI':>4} {'ATR':>6} {'vol3/20':>7}")
    feats = {}
    for p, r in zip(pos, rets):
        if p not in usable:
            continue
        f = entry_features(data[p["id"]], p["opened_at"], p["entry_price"])
        feats[p["id"]] = f
        if f["n_prior"] < 5:
            print(f"  {p['symbol'][:10]:10} only {f['n_prior']} candles before entry")
            continue
        print(f"  {p['symbol'][:10]:10} {(p['strategy'] or '')[:8]:8} {pct(r)} {pct(f['runup5'])} {pct(f['runup15'])} "
              f"{pct(f['runup60'])} {pct(f['from_high15'])} {pct(f['vwap_dist'])} {pct(f['ema21_dist'])} "
              f"{f['rsi']:4.0f} {f['atr'] * 100:5.1f}% {f.get('vol_ratio', float('nan')):7.2f}")
    win_f = [feats[p["id"]] for p, r in zip(pos, rets) if r > 0 and feats.get(p["id"], {}).get("n_prior", 0) >= 5]
    los_f = [feats[p["id"]] for p, r in zip(pos, rets) if r <= 0 and feats.get(p["id"], {}).get("n_prior", 0) >= 5]
    for key, label in (("runup15", "15m run-up"), ("vwap_dist", "above VWAP"), ("atr", "1m ATR"),
                       ("from_high15", "vs 15m high")):
        wv = [f[key] for f in win_f if f.get(key) is not None]
        lv = [f[key] for f in los_f if f.get(key) is not None]
        print(f"  median {label:12}: winners {pct(median(wv)) if wv else '   n/a'} (n={len(wv)}) | "
              f"losers {pct(median(lv)) if lv else '   n/a'} (n={len(lv)})")
    chase = [r for p, r in zip(pos, rets) if (feats.get(p["id"], {}).get("vwap_dist") or 0) > 0.20]
    if chase:
        print(f"  entries > 20% above VWAP: {len(chase)} trades, avg result {pct(mean(chase))}")

    # ---- D. path after entry
    print("\n=== D. PATH AFTER ENTRY (vs entry price, regardless of when the bot exited) ===")
    print(f"  {'token':10} {'result':>7} | {'15m MFE':>8} {'15m MAE':>8} {'first':>11} | {'60m MFE':>8} "
          f"{'60m MAE':>8} {'first':>11} | {'5m MAE':>7}")
    for p, r in zip(pos, rets):
        if p not in usable:
            continue
        c = data[p["id"]]
        a15 = path_after(c, p["opened_at"], p["entry_price"], 15)
        a60 = path_after(c, p["opened_at"], p["entry_price"], 60)
        a5 = path_after(c, p["opened_at"], p["entry_price"], 5)
        if not a15:
            continue
        print(f"  {p['symbol'][:10]:10} {pct(r)} | {pct(a15['mfe'])} {pct(a15['mae'])} {a15['order']:>11} | "
              f"{pct(a60['mfe'])} {pct(a60['mae'])} {a60['order']:>11} | {pct(a5['mae']) if a5 else '   n/a'}")

    # ---- E. after stop-loss exits
    print("\n=== E. AFTER STOP-LOSS EXITS (candles strictly after the exit minute) ===")
    stopped = [p for p in usable if (p["exit_reason"] or "").startswith("stop loss")]
    rec = []
    for p in stopped:
        exit_px = p["realized_usd"] / p["cost_usd"] * p["entry_price"]  # effective exit incl. costs
        start = (int(p["closed_at"]) // 60) * 60 + 60
        w15 = path_after(data[p["id"]], start, p["entry_price"], 15)
        w60 = path_after(data[p["id"]], start, p["entry_price"], 60)
        if not w60:
            print(f"  {p['symbol'][:10]:10} no candles after exit yet")
            continue
        rec.append((p, w15, w60, exit_px))
        print(f"  {p['symbol'][:10]:10} exit {pct(exit_px / p['entry_price'] - 1)} | next 15m: high {pct(w15['mfe'])} "
              f"low {pct(w15['mae'])} | next 60m: high {pct(w60['mfe'])} low {pct(w60['mae'])} ({w60['order']}, "
              f"{w60['mins']:.0f}m data)")
    if rec:
        n = len(rec)
        back = sum(1 for _, _, w, _ in rec if w["mfe"] >= 0)
        back_clean = sum(1 for _, _, w, _ in rec if w["mfe"] >= 0 and (w["order"] != "low first" or w["mae"] > -0.25))
        print(f"  recovered to entry within 60m: {back}/{n} | of those, without first falling below -25%: {back_clean}/{n}")
        print(f"  fell below -25% vs entry within 60m: {sum(1 for _, _, w, _ in rec if w['mae'] <= -0.25)}/{n} | "
              f"below -50%: {sum(1 for _, _, w, _ in rec if w['mae'] <= -0.50)}/{n}")

    # ---- F. replay
    print("\n=== F. EXIT-RULE REPLAY OF ALL TRADES (same entries, 2% round-trip cost) ===")
    print("  optimistic = fill at stop level; pessimistic = fill at min(stop, candle close) - models crashes.")
    print("  R = return / planned stop distance (equal-$-risk view).")
    print(f"  {'rule':38} {'n':>2} {'win':>4} {'exp opt':>8} {'exp pess':>8} {'PF pess':>7} {'MDD pess':>8} "
          f"{'avgR':>6}  90% CI (pess)")
    base_pess = None
    for name, ov in RULES.items():
        o_res, p_res = [], []
        for p in usable:
            a = (feats.get(p["id"]) or {}).get("atr")
            o = simulate(data[p["id"]], p["entry_price"], p["opened_at"], ov, a, False)
            q = simulate(data[p["id"]], p["entry_price"], p["opened_at"], ov, a, True)
            if o and q:
                o_res.append(o)
                p_res.append(q)
        if not p_res:
            continue
        so, sp = stats([x["ret"] for x in o_res]), stats([x["ret"] for x in p_res])
        ci = bootstrap_ci([x["ret"] for x in p_res])
        avg_r = mean(x["ret"] / x["risk"] for x in p_res)
        if base_pess is None:
            base_pess = sp["exp"]
        pf = "  -  " if sp["pf"] is None else f"{sp['pf']:5.2f}"
        print(f"  {name:38} {sp['n']:2} {sp['win']:4.0%} {pct(so['exp'])} {pct(sp['exp'])} {pf:>7} "
              f"{pct(sp['mdd'])} {avg_r:6.2f}  [{pct(ci[0])}, {pct(ci[1])}]")
    actual_usable = [(p["realized_usd"] - p["cost_usd"]) / p["cost_usd"] for p in usable]
    print(f"\n  Fidelity check: rule A pessimistic exp {pct(base_pess)} vs bot's actual {pct(mean(actual_usable))} "
          f"on the same {len(usable)} trades.")
    print("  Replay limits: 1-minute resolution (bot polls every 5s), no chart/dump exits, no failed-transaction")
    print("  model, fixed 2% cost. Rules were NOT pre-registered: picking the best row on this sample overfits.")
    print("\nRead-only study finished. Database not modified. Candle cache: " + CACHE_DIR)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else "paper-run1.db", refresh="--refresh" in sys.argv)
