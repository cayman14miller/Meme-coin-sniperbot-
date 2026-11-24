# Research log

Every strategy version is compared against the frozen baseline below under the same conditions.
Numbers come from `research/baseline_study.py` (read-only). Small samples are labelled as such.

## v0 baseline (frozen) - commit 5472e47, paper run `paper-run1.db`

Conditions: default settings (10% stop, TP1 +40% half, TP2 +100%, trail 15/15, min score 65,
$25/trade), paper broker (0.25% fee + 0.5% base slippage + size/liquidity impact), one session
of ~45 minutes, 10 closed trades. **Sample is far too small for conclusions; directional only.**

| Metric | Value |
|---|---|
| Trades | 10 |
| Win rate | 10% (1/10) |
| Avg win / avg loss | +68.8% / -21.6% |
| Expectancy | **-12.6% per trade** (-$3.14), 90% bootstrap CI [-29.0%, +4.1%] |
| Profit factor | 0.35 |
| Total | -$31.44 |
| Max drawdown | -1.63 positions (cumulative) |
| Expectancy excl. the one rug | -4.4% |
| Kill switch | tripped correctly on 5th consecutive loss; 0 buys after trip |

By strategy: momentum 0/5 (-13.2% avg), early 1/4 (+6.5% avg, entirely one trade), dip 0/1 (-86.1%, rug).

### Findings

1. **The candidate universe is dominated by pump-and-dumps.** 5 of 10 coins fell more than 50% within
   60 minutes of our entry (UCASH -87%, GOOBIE -74%, GTS1 -66%, JENSEN -53%, and the one winner GROKIA
   -90% after we sold). Exits cannot fix a universe where the base rate of collapse is this high.
2. **Entries come after the pump.** 7/10 entries were more than 20% above VWAP; median 15-minute
   run-up before entry was +38% for losers; 60-minute run-ups up to +214%. Momentum went 0/5.
   The single winner had the most extreme run-up (+88% in 15m), so "anti-chase" alone is not
   proven either - with one winner no entry feature separates winners from losers yet.
3. **Volatility is extreme relative to the stop.** 1-minute ATR at entry ranged 3.7%-36.9% (median ~8%).
   6/10 trades were down 14%+ within 5 minutes. A 10% stop on a coin moving 20-37% per minute is noise.
4. **Missed recoveries are real but not capturable.** 7/9 stopped coins got back to entry within 60m,
   but only 4/9 without first falling below -25%; 8/9 fell below -25% and 4/9 below -50%.
5. **No exit rule produced positive expectancy on these entries.** Best pessimistic expectancy:
   early trail (activate +5%, trail 8%) -9.3% [CI -13.8%, -4.6%]; current rule -19.1%.
   Wider fixed stops (15-25%), ATR x3 and close-confirmation were no better or worse.
   ATR x4 (10-30%) was the only rule catching runners (30% win rate) but still -12.3%.
6. **The rug (UCASH) showed nothing unusual in price data before entry** (VWAP +1.5%, ATR 6.2%, 39h old).
   Detecting it needs non-price data (liquidity/holder changes) - not available in candles.
7. **Method limitation found:** with 1-minute ranges larger than the trail distance, candle replay cannot
   order events inside a candle; optimistic vs pessimistic fills bracket the truth (actual -12.6% sits
   between replay -11.2% and -19.1%). Tight-exit rules need 5-second price data to evaluate.

### Implications for the research plan

- Biggest weakness: **what we buy and when**, not how we exit.
- Needed next: an unbiased recorder of *every* candidate (not just traded ones) with forward outcomes,
  to measure base rates (how often candidates dump, by discovery source / age / features) and test
  entry rules out-of-sample; plus 5-second price logging for exit research.

## Reliability & security audit (cycle 2) - no change to entry/exit decisions

| # | Severity | Weakness found | Fix | Test |
|---|---|---|---|---|
| R1 | critical (live) | A live swap could land on-chain while our code saw an error/timeout -> tokens held with no record and no stop loss | Write-ahead `pending` position before every buy; on error the wallet balance is checked and the holding is adopted (or marked `failed`); unresolved buys are re-checked every monitor tick and on startup | `test_uncertain_live_buy_*`, `test_crash_mid_buy_is_reconciled_on_startup` |
| R2 | high | If DexScreener returned no price for a holding, its stop loss was silently skipped forever | Jupiter price fallback (stop/TP still enforced); if no source prices it for 60s a `risk` push alert fires every 5 min | `test_missing_dexscreener_price_falls_back_to_jupiter`, `test_no_price_at_all_raises_a_risk_alert` |
| R3 | high | Process death leaves positions unprotected, silently | launchd templates with auto-restart + caffeinate (`deploy/`); optional `HEARTBEAT_URL` dead-man's switch; startup push lists positions being re-protected | `test_decisions_and_ticks_are_logged` (heartbeat) |
| R5 | medium | Drawdown kill switch measured in USD: SOL halving tripped it with zero trading loss (verified) | Drawdown measured in SOL | `test_sol_price_crash_does_not_trip_drawdown` |
| R6 | medium | Dashboard bound to 0.0.0.0 over plain HTTP; unlimited token guessing | Default `HOST=127.0.0.1`; 10 failed attempts / 5 min per address -> 429 lockout | `test_dashboard_brute_force_is_rate_limited` |
| R7 | medium (live) | `MODE=live` alone enabled real trading; dashboard could raise trade size without bound | Live requires `LIVE_TRADING_CONFIRM` phrase, env hard caps `LIVE_MAX_TRADE_SOL` / `LIVE_MAX_DEPLOYED_SOL` (not editable from dashboard), strong dashboard token | `test_live_mode_refuses_to_start_without_interlocks`, `test_live_hard_cap_blocks_oversized_trade` |
| R10 | high (research) | No record of decisions or of the prices the bot saw | `decisions` table (every researched coin: inputs, scores, reasons, action) and `price_ticks` (every 5s monitor price + source) | `test_decisions_and_ticks_are_logged` |

Known remaining gaps: no on-chain stop orders exist for these DEX pools, so protection still requires the
process to be running; a wallet-wide scan for tokens held without any position record is not implemented
(only buys the bot attempted are reconciled); live execution path (JupiterBroker) has never been exercised
against mainnet and must be tested with tiny amounts only after explicit approval.

## Execution cost measurement (cycle 2) - live Jupiter quotes, 30 coins, Nov 24 2026

Round trip (buy then immediately sell, quotes only) by trade size and pool liquidity:

| Liquidity | $25 median | $100 median |
|---|---|---|
| < $15k | 3.45% | 6.58% |
| $15-50k | 1.82% | 2.38% |
| $50-150k | 1.87% | 2.13% |

- Pump.fun AMM pools (most of the universe) cost ~0.9-1% **per side** even with negligible impact;
  Meteora / Raydium CLMM pools ~0-0.5%. One coin (NVIDIA, $40k liquidity) had **no route** at all.
- The v0 paper broker charged 0.25% per side and computed impact against total (not SOL-side) liquidity:
  it understated Pump.fun fees ~4x and impact ~2x. Paper fills now use live quotes (fallback: a model
  calibrated on these measurements). **v0's paper results were therefore optimistic on costs.**
- Not yet measured: failed-transaction rate, latency slippage, MEV/sandwich losses (a 3% slippage
  tolerance invites sandwiching up to that amount). These need tiny live tests, only with approval.
- Implication: at ~2% round trip plus a 10% stop, a strategy needs a substantial edge per trade; trade
  size should stay small relative to liquidity.

## First recorder results (cycle 3) - FIRST HALF ONLY, per pre-registered protocol

Data: `market-run1.db` (1.1h, ~1,150 tokens) and `market-run2.db` (1.2h, ~1,250 tokens, adds Jupiter stats),
recorded concurrently on 2026-11-24 04:10-05:30 UTC. **The two runs cover the same hour, so run 2 is not an
independent replication.** One market regime only. Second halves not yet examined.

### Data-quality finding (fixed before any conclusion)
The first pass showed +18% expectancy for v0-eligible coins. It was an artifact: after a pool's liquidity is
pulled, DexScreener keeps the last traded price (stale) and tiny trades in drained pools print phantom prices
(e.g. $0.0000149 -> $0.311 with $84 liquidity). Raw responses confirmed real drains (SOL side ~1e-7 SOL).
Studies now treat pools under $2k liquidity as untradeable (never credit a gain; value capped at the SOL side).

### Base rates (first half, 60-minute outcomes)
| Universe | n | dumped >=50% in 60m | ran >=50% in 60m | median return 60m | v0 exits, mean |
|---|---|---|---|---|---|
| Every newly seen token (run 1) | 470 | 31% | 10% | -1.5% | -11.7% |
| Tokens passing v0 static filters (run 1) | 38-49 | **82%** | 37% | **-77%** | -9.0% |
| Tokens passing v0 static filters (run 2) | 45-50 | **84%** | 40% | **-82%** | -12.5% |

- Among v0-eligible coins, 41/41 young (<30 min) pools had Jupiter organic score **< 10 ("low")**: activity is
  bot / wash-driven. 92% of them dumped >=50% within an hour; about half spiked >=50% first (pump-and-dump).
- v0's static filters on brand-new pools (>= $15k liquidity, rising volume, buys > sells) select mainly
  **manufactured-activity pools that are then drained**. v0's RugCheck step rejects most (42 of 50 were
  RugCheck "danger"); the entry replay measures the remainder.

### Pre-registered hypotheses - first-half verdicts
| ID | Verdict (half 1) | Evidence |
|---|---|---|
| H1 chasing | **supported** | 1h change >100%: dump 51%, v0 exp ~-8.5% (n=53) vs 0-100%: dump 35%, exp ~-2.8% (n=187) |
| H2 wash (Gecko tx/trader > 4) | **supported on dump rate** | dump 42% (n=109) vs 30% (n=350); expectancy not different |
| H2 wash (Jupiter organic) | not testable as a contrast | every eligible coin was "low" - no comparison group yet |
| H3 serial deployers | not evaluable | buckets n < 30 |
| H4 concentration | not evaluable | buckets n < 30 |
| H5 real participation | not evaluable | no coin met the criteria |
| H6 liquidity pulls | not evaluable | no data in window |
| H7 paid promotion | **not supported** | boosted n=16 (< 30) and direction opposite (6% vs 32% dump) |
| H8 forming-candle fix | pending | entry replay |
| H9 very young pairs | **supported** | < 30 min: 34% dump (n=434) vs older: ~7% (n=56) |

Exploratory (not pre-registered, must be confirmed on fresh data): RugCheck "danger" tokens dumped 82% vs 16% for
"ok" (n=60 vs 33) in the full universe.

### New reliability finding
**Stale price after liquidity removal hides rugs from the stop loss.** When a pool is drained, DexScreener keeps
reporting the last traded price, so a price-only stop never fires and the position looks healthy while worthless.
The old paper broker would even "sell" at that stale price (understating rug losses). Quote-based paper fills fail
correctly (no route). Fix needed: monitor pool liquidity for every open position and treat a collapse as an exit /
alert condition.

### Entry replay - first half of run 1 (entries with >= 60 min follow-up, calibrated costs, drained-pool rule)

| Rule | trades | win | mean net | 90% CI | main exits |
|---|---|---|---|---|---|
| v0 (bot as is) | 5 | 20% | **-36.6%** | [-66.9%, -8.8%] | stop 4, trail 1 |
| v1_scoring (H8) | 5 | 0% | -43.9% | [-70.2%, -18.3%] | stop 5 |
| all v0-eligible | 55 | 38% | **-14.2%** | [-30.1%, +2.6%] | **liquidity collapsed 24**, TP2 16, stop 10, trail 5 |

- No rule shows positive expectancy. H8 not evaluable (n=5). 44% of "buy everything eligible" trades ended in a
  liquidity pull. v1 rule pre-registered in HYPOTHESES.md; to be tested on fresh data only.

### Out-of-sample test of the pre-registered v1 rule - run 3 (recorded 06:17-08:04 UTC, after registration)

Run exactly as registered, no rule changes. Fresh data: 107 min, 1644 tokens seen, 134 passed v0 filters.

| Rule | trades | win | mean net | 90% CI | mean net, costs +50% | 90% CI (+50%) |
|---|---|---|---|---|---|---|
| v0 (bot as is) | 7 | 43% | +3.3% | [-9.5%, +18.8%] | +2.2% | [-10.5%, +17.7%] |
| **v1** (registered rule) | **6** | 33% | +2.6% | [-10.2%, +21.7%] | +1.5% | [-11.2%, +20.6%] |
| v1_filters_only | 7 | 29% | +6.0% | [-9.1%, +27.3%] | +4.9% | [-10.2%, +26.2%] |
| all v0-eligible | 83 | 24% | **-31.0%** | [-41.2%, -20.4%] | -31.8% | [-42.0%, -21.2%] |

**Verdict against the fixed criteria: v1 FAILS / inconclusive - not deployed.**
- n = 6, below the required 30.
- v1 did not beat v0 by >= 3 pp (+2.6% vs +3.3%); it beat all_eligible by ~34 pp.
- CI lower bound is far below 0 (with and without +50% costs). Nothing here is evidence of profitability.
- Run 1 half 2 (the protocol's secondary check) is not evaluable: every rule entry in that half was too recent
  for 60 min follow-up.

What the run does show (descriptive, n is large only for all_eligible):
- Buying every coin that passes v0's static filters lost **-31% per trade**; 46 of 83 exits (55%) were liquidity
  pulls. The filtered universe is dominated by rugs, consistent with runs 1-2.
- The scoring/safety layers (v0, v1) avoided almost all of that: their 6-7 trades had no liquidity-collapse
  exits. That is the one consistent signal across runs, but it rests on tiny samples.
- Sample-size arithmetic: v0/v1 fire ~3-4 resolvable trades per recorded hour, so >= 30 trades needs roughly
  8-10 hours of recording, and a CI narrow enough to separate +3% from 0 needs several hundred trades (multi-day).
  Next: keep recording fresh runs (run 4 started 09:47 UTC) and pool all post-registration runs for one evaluation.

### Pooled out-of-sample - INTERIM look (runs 3 + 4, 3.7 h fresh data) - cannot pass v1, n < 30

| Rule | trades | win | mean net | 90% CI | mean net, costs +50% |
|---|---|---|---|---|---|
| v0 | 15 | 40% | -4.8% | [-18.1%, +9.3%] | -5.9% |
| v1 | 11 | 36% | +2.1% | [-10.0%, +16.1%] | +1.1% |
| v1_filters_only | 14 | 36% | +3.1% | [-7.9%, +16.4%] | +2.1% |
| all v0-eligible | 160 | 28% | **-27.9%** | [-35.7%, -20.0%] | -28.7% |

- v1 is ~7 pp ahead of v0 on the point estimate, but the CIs overlap almost entirely; at n=11 this is noise-level.
- Medians are negative for every rule (-6% to -9%): a few winners carry the means. Win rates 36-40%.
- Only stable, large effect: the unfiltered eligible universe loses ~28%/trade (both runs, n=160).
- Decision deferred until pooled v1 n >= 30 (amendment in HYPOTHESES.md). Run 5 recording next.

### Pooled out-of-sample - INTERIM look 2 (runs 3-5, 5.6 h fresh data) - v1 n=18 < 30, no decision

| Rule | trades | win | mean net | median | 90% CI | mean net, costs +50% |
|---|---|---|---|---|---|---|
| v0 | 27 | 33% | -2.5% | -12.6% | [-11.9%, +7.0%] | -3.6% |
| v1 | 18 | 28% | -0.6% | -13.0% | [-11.0%, +11.6%] | -1.6% |
| v1_filters_only | 23 | 26% | -1.5% | -13.0% | [-10.8%, +9.2%] | -2.6% |
| all v0-eligible | 235 | 27% | **-28.3%** | -20.5% | [-34.9%, -21.6%] | -29.1% |

- v1's lead over v0 shrank from ~7 pp (interim 1) to ~2 pp - regression toward v0, as expected for noise.
- All scored rules sit near zero expectancy with negative medians (~-13%: the typical trade is stopped out;
  a few trail/TP2 winners pay for them). No evidence of an edge in either direction.
- The scoring + RugCheck layer is worth ~25 pp/trade vs buying the filtered universe - stable in all 3 runs.
- Run 5: 28/94 cycles had 1 failed request (~1-2% of requests); missing snapshots can only delay exits.

### Pooled out-of-sample - INTERIM look 3 (runs 3-6, 7.5 h fresh data) - v1 n=22 < 30, no decision

| Rule | trades | win | mean net | median | 90% CI | mean net, costs +50% |
|---|---|---|---|---|---|---|
| v0 | 36 | 28% | **-6.9%** | -13.5% | [-15.8%, +1.7%] | -8.0% [-17.0%, +0.6%] |
| v1 | 22 | 27% | -1.3% | -13.4% | [-10.2%, +8.6%] | -2.4% |
| v1_filters_only | 31 | 29% | -1.6% | -12.5% | [-8.8%, +6.6%] | -2.6% |
| all v0-eligible | 283 | 28% | **-26.5%** | -17.6% | [-32.6%, -20.5%] | -27.4% |

- v0 (the code currently paper-trading on the user's Mac) is drifting clearly negative: -6.9%/trade, CI upper
  bound only +1.7%. Consistent with the frozen v0 baseline (-12.6%, n=10).
- v1 is ~5.6 pp ahead of v0 but its own expectancy is ~-1%, CI [-10%, +9%]: no evidence of an edge.
- Run 6: 20/92 cycles had 1 failed request.

## FORMAL out-of-sample decision on v1 (runs 3-7, 9.4 h fresh data, first pooled look with v1 n >= 30)

| Rule | trades | win | mean net | median | 90% CI | mean net, costs +50% | 90% CI (+50%) |
|---|---|---|---|---|---|---|---|
| v0 (bot as is) | 50 | 32% | -4.7% | -13.1% | [-11.6%, +2.6%] | -5.8% | [-12.8%, +1.4%] |
| **v1** | **32** | 25% | **-1.3%** | -13.1% | [-8.7%, +6.7%] | -2.4% | [-9.8%, +5.6%] |
| v1_filters_only | 42 | 31% | +1.5% | -12.4% | [-5.4%, +8.6%] | +0.4% | [-6.5%, +7.5%] |
| all v0-eligible | 349 | 29% | -24.7% | -16.6% | [-29.9%, -19.2%] | -25.5% | [-30.8%, -20.1%] |

Pre-registered criteria:
| Criterion | Result |
|---|---|
| >= 30 trades | **pass** (32) |
| mean beats v0 by >= 3 pp | **pass, barely** (+3.4 pp; +3.4 pp with costs +50%) |
| mean beats all_eligible by >= 3 pp | **pass** (+23 pp) |
| profitable: 90% CI lower bound > 0, incl. costs +50% | **FAIL** (-8.7% / -9.8%) |

**Verdict: v1 is NOT profitable and is NOT deployed live.** It meets the "better than v0" point-estimate bar, but:
- bootstrap of the difference v1 - v0: +3.4 pp, 90% CI [-6.9, +13.6] pp, P(no improvement) ~ 0.30 - the
  improvement is not statistically established;
- 23 of v1's 32 trades are trades v0 also took; the 27 v0 trades v1 rejected averaged -7.3% (directionally right);
- v1's mean is carried by 3 winners (+54%, +55%, +72%); without the single best trade it is -3.7%;
- 20 of 32 v1 exits are the 10% stop: the typical entry falls right after the buy.

v1_filters_only (+1.5%, n=42) is reported for completeness; it was a registered comparison variant, not the
decision rule, and its CI also spans zero. Promoting it now would be choosing the winner after seeing the data.

**What this establishes (high confidence, n=349):** buying what passes v0's static filters loses ~25%/trade;
the scoring + RugCheck layer removes most of that loss. **What it does not establish:** any positive edge.
Every scored rule is consistent with zero to slightly negative expectancy after realistic costs.
