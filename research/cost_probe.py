"""Measure real execution cost with live Jupiter quotes (read-only - quotes only, nothing is signed or sent).

    python research/cost_probe.py market-data.db [--sizes 25,100] [--n 40]

For coins the recorder is currently tracking, asks Jupiter for a buy quote (SOL -> token) of $X and then a sell
quote for exactly the tokens that buy would return (token -> SOL). Round-trip cost = 1 - SOL back / SOL in:
LP fees + price impact on both legs, as the aggregator would actually route it right now. Not included:
priority/base network fees, failed transactions, and price movement during the seconds a real trade takes.
"""
from __future__ import annotations

import json
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from statistics import mean, median

try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    CTX = ssl.create_default_context()

SOL = "So11111111111111111111111111111111111111112"
QUOTE = "https://lite-api.jup.ag/swap/v1/quote"


def quote(inp: str, out: str, amount: int) -> dict | None:
    url = QUOTE + "?" + urllib.parse.urlencode({"inputMint": inp, "outputMint": out, "amount": amount,
                                                "slippageBps": 500, "restrictIntermediateTokens": "true"})
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cost-probe"})
            with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return {"error": e.read().decode()[:200]}
        except Exception as e:  # network
            return {"error": str(e)}
    return {"error": "rate limited"}


def main(db: str, sizes: list[float], n: int) -> None:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    sol_usd = con.execute("SELECT usd FROM sol_price ORDER BY ts DESC LIMIT 1").fetchone()[0]
    rows = con.execute("""SELECT d.mint, d.liq_usd, d.dex, t.symbol FROM dex_snaps d JOIN tokens t ON t.mint=d.mint
                          WHERE d.ts > (SELECT MAX(ts) FROM dex_snaps) - 180 AND d.liq_usd >= 5000
                          GROUP BY d.mint ORDER BY d.liq_usd""").fetchall()
    con.close()
    if not rows:
        sys.exit("No liquid coins in the last 3 minutes of recorder data.")
    step = max(len(rows) // n, 1)
    sample = rows[::step][:n]  # spread across the liquidity range
    print(f"COST PROBE - {len(sample)} coins, SOL ${sol_usd:.2f}, sizes {sizes} USD")
    print(f"  {'symbol':10} {'liquidity':>11} {'dex':10} " + " ".join(f"{'$' + str(int(s)) + ' rt':>9}" for s in sizes)
          + "   route")
    results = {s: [] for s in sizes}
    buckets = {s: {} for s in sizes}
    for mint, liq, dex, sym in sample:
        cells, route = [], ""
        for s in sizes:
            lamports = int(s / sol_usd * 1e9)
            b = quote(SOL, mint, lamports)
            time.sleep(1.1)
            if not b or "outAmount" not in b:
                cells.append("  no-route")
                continue
            route = "+".join(r["swapInfo"]["label"] for r in b.get("routePlan", []))
            q = quote(mint, SOL, int(b["outAmount"]))
            time.sleep(1.1)
            if not q or "outAmount" not in q:
                cells.append("   no-sell")
                results[s].append(1.0)  # could not exit at all
                continue
            rt = 1 - int(q["outAmount"]) / lamports
            results[s].append(rt)
            key = "<15k" if liq < 15e3 else "15-50k" if liq < 50e3 else "50-150k" if liq < 150e3 else ">150k"
            buckets[s].setdefault(key, []).append(rt)
            cells.append(f"{rt * 100:8.2f}%")
        print(f"  {(sym or '?')[:10]:10} {liq:11,.0f} {(dex or '')[:10]:10} " + " ".join(cells) + f"   {route[:40]}")
    print()
    for s in sizes:
        r = results[s]
        if not r:
            continue
        print(f"  ${s:g} round trip: median {median(r):.2%}, mean {mean(r):.2%}, worst {max(r):.2%} (n={len(r)})")
        for k in ("<15k", "15-50k", "50-150k", ">150k"):
            if k in buckets[s]:
                v = buckets[s][k]
                print(f"      liquidity {k:8}: median {median(v):.2%} (n={len(v)})")
    print("\nNot included: network/priority fees (~0.0001-0.001 SOL per tx), failed transactions, latency.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(a.lstrip("-").split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    main(args[0] if args else "market-data.db",
         [float(x) for x in opts.get("sizes", "25,100").split(",")], int(opts.get("n", 40)))
