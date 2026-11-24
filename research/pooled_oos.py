"""Pooled out-of-sample evaluation of the pre-registered rules over several recorder runs.

    python -m research.pooled_oos run3.db run4.db ... [--rules=v0,v1,v1_filters_only,all_eligible]

Each run's trades are saved next to its DB (<db>.trades.json) so a run is replayed once - candle fetches are
slow and the GeckoTerminal cache expires. Delete that file to force a re-replay.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.entry_replay import COST_STRESS, replay, summarize  # noqa: E402

RULES = "v0,v1,v1_filters_only,all_eligible"


def run_trades(db: str, rules: list[str], size: float = 25.0, replay_fn=replay) -> dict:
    path = db + ".trades.json"
    if os.path.exists(path):
        saved = json.load(open(path))
        if set(rules) <= set(saved["trades"]):
            return saved
    trades, fired, unresolved, n_tok, span = replay_fn(db, rules, size)
    out = dict(db=db, span=list(span), eligible_tokens=n_tok, fired=fired, unresolved=unresolved, trades=trades)
    json.dump(out, open(path, "w"))
    return out


def main(dbs: list[str], rules: list[str]) -> dict:
    runs = [run_trades(db, rules) for db in dbs]
    hours = sum((r["span"][1] - r["span"][0]) / 3600 for r in runs if r["span"][0])
    print(f"POOLED OUT-OF-SAMPLE - {len(runs)} runs, {hours:.1f}h of data, $25/trade")
    for r in runs:
        print(f"  {os.path.basename(r['db'])}: " + ", ".join(f"{rn} {len(r['trades'][rn])}" for rn in rules))
    pooled = {rn: sorted((x for r in runs for x in r["trades"][rn]), key=lambda x: x["t"]) for rn in rules}
    for rn in rules:
        ts = pooled[rn]
        print(f"\n=== rule {rn}: {len(ts)} trades with >=60m follow-up ===")
        summarize("all", ts)
        summarize("all, costs +50%", [dict(x, net=x["gross"] - COST_STRESS * x["cost"]) for x in ts])
        for r in runs:
            summarize(os.path.basename(r["db"]), r["trades"][rn])
    n = len(pooled.get("v1", []))
    print(f"\nv1 pooled n = {n}: " + ("FORMAL evaluation (n >= 30)" if n >= 30 else
                                    "INTERIM look only - cannot pass v1 (n < 30)"))
    return pooled


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(a.lstrip("-").split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    main(args, opts.get("rules", RULES).split(","))
