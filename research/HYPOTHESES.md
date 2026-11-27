# Pre-registered hypotheses (written before any recorder outcome data was analysed)

Registered: 2026-11-24 ~04:30 UTC, after the v0 baseline (10 paper trades) and before the first
`universe_study` / `entry_replay` run on recorder data (`market-run1.db`, `market-run2.db`).

Purpose: decide in advance what we test and what counts as evidence, so filters are not fitted to noise.

## Hypotheses

| ID | Hypothesis (direction stated in advance) | Measured by |
|---|---|---|
| H1 | **Chasing hurts.** Candidates whose 1h price change is > +100% at decision time have a higher 60m dump rate and worse v0-exit expectancy than those at 0-100% | universe_study `by 1h price change`, entry_replay with filter |
| H2 | **Wash trading hurts.** Low Jupiter organic score (label `low` or score < 30) or > 4 transactions per unique trader (GeckoTerminal, 5m) -> higher dump rate, worse expectancy | jup_snaps.organic_*, gecko_snaps traders |
| H3 | **Serial deployers hurt.** Creator with >= 50 previous launches (`devMints`) -> higher dump rate | jup_snaps.dev_mints |
| H4 | **Concentration hurts.** Top holders > 30% of supply -> higher dump rate | jup_snaps.top_holders_pct |
| H5 | **Real participation helps.** Positive 5m holder change with net buyers > 0 and >= 10 organic buyers -> better forward 15/60m returns | jup_snaps m5 holder/organic fields |
| H6 | **Liquidity pulls precede dumps.** 5m liquidity change < -10% -> higher dump rate | jup_snaps.m5_liq_chg |
| H7 | **Paid promotion is adverse selection.** Tokens first seen via DexScreener boosts dump more than those first seen via GeckoTerminal trending/new pools | universe_study `by first discovery source` |
| H8 | **The forming-candle bug inflates momentum scores.** Scoring with completed candles only + requiring volume confirmation reduces losing momentum entries without reducing expectancy | entry_replay v0 vs v1-scoring |
| H9 | **Very young pairs are the riskiest.** Pairs < 30 min old have the highest dump rate | universe_study `by pair age` |

## Decision protocol

1. Data is split chronologically. Hypotheses are evaluated on the **first half** only.
2. A hypothesis is "supported" only if the effect has the predicted direction, each compared bucket has
   n >= 30 decision points, and the difference in dump rate is >= 10 percentage points (or expectancy >= 3 pp).
3. Supported hypotheses are combined into **one** v1 entry rule, written down before looking at the second half.
4. v1 is evaluated **once** on the second half and then on **fresh forward data** (new recorder runs / paper
   trading). A rule that fails on fresh data is dropped, not re-tuned on the same data.
5. With 9 hypotheses, ~0.5 false positives are expected at 5% significance; replication on fresh data is
   required before any rule is trusted.
6. Cost assumptions: calibrated model (Pump.fun AMM ~0.95%/side + impact vs SOL side + network fees);
   results must also hold with costs +50% (sensitivity) to count.

## v1 rule - registered 2026-11-24 ~06:00 UTC, BEFORE any out-of-sample data was examined

Built only from hypotheses supported on half 1 of `market-run1.db` (see RESULTS.md):

* keep v0 static filters and v0 RugCheck requirement (ok, score <= 5000)
* **H9** pair age >= 30 minutes
* **H1** 1-hour price change <= +100% at decision time
* **H2** <= 4 transactions per unique trader in the last 5 minutes (GeckoTerminal); missing data = reject
* scoring unchanged from v0 (H8 was not evaluable: only 5 trades per rule)

Implemented as `entry_replay` rules `v1` (filters + v0 scoring) and `v1_filters_only` (filters, no scoring).

**Test plan (fixed now):** evaluate once on fresh recorder data collected after this registration (new runs),
against `v0` and `all_eligible` on the same data, with entries that have >= 60 min follow-up, calibrated costs,
and the drained-pool rule. v1 is considered better only if it has >= 30 trades and its mean net return exceeds
v0's and all_eligible's by >= 3 percentage points; it is considered *profitable* only if the lower bound of the
90% bootstrap CI of its mean is above 0 - with costs +50% as well. Otherwise it is not deployed.

### Amendment - 2026-11-24 ~11:45 UTC, written after run 3 (v1 n=6) and BEFORE run 4 was analysed

Run 3 alone was far below n >= 30, so the registered test is applied to **pooled** post-registration runs
(run 3, run 4, and later runs), with the rule unchanged. To avoid optional stopping, the **formal, one-time
decision** is taken at the first pooled evaluation where v1 has >= 30 trades; looks before that are interim,
reported for transparency, and cannot pass v1. Criteria are unchanged (vs v0 and all_eligible, +3 pp, 90% CI
lower bound > 0 with costs +50%). Run 1 half 2 is excluded (no resolvable rule entries).

### Outcome (2026-11-24 ~21:15 UTC): v1 formally evaluated on runs 3-7 - better than v0 on the point estimate
(+3.4 pp, not statistically established), NOT profitable (CI lower bound -8.7%). Not deployed. Per protocol, v1 is
not re-tuned on this data; any new rule must be registered before it sees fresh data (run 8 onward).

## Replication test of v0 by forward paper trading - registered 2026-11-27, before the run starts

Motivation: Mac paper run 1 (98 trades) showed v0 at +4.3%/trade after cost correction, CI [-2.4%, +11.1%].
Test: a NEW paper run with the current code (realistic fills from live Jupiter quotes, calibrated costs,
liquidity-pull exits), v0 strategy and all risk settings unchanged, new database. Kill-switch trips are resumed
manually as before (no automatic restarts).

* Primary metric: mean net P&L % per closed trade (as booked by the quote-based paper broker).
* v0 counts as **replicated-profitable** only if, at >= 200 closed trades, the mean is > 0 AND the 90% bootstrap
  CI lower bound is > 0, AND the mean stays > 0 with costs +50% (each trade minus an extra 50% of its fees+impact),
  AND the mean without the best 5 trades is > 0.
* Interim looks before 200 trades are reported but cannot pass. No parameter changes during the run; if anything
  is changed, the run restarts with a new database and the count restarts.
* Live trading is not considered before this passes, and then only with the user's explicit approval and tiny size.
