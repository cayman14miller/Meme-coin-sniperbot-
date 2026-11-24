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
