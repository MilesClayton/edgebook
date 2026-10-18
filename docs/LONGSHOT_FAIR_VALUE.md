# Does the engine's fair value pick better long-shot sales?

**Answer: not yet.** A pre-registered rule that demands a bigger fair-value margin (edge
above 0.5c instead of above 0) did better than the current rule on every logged long-shot
firing: +2.9c per contract, 95% interval −0.1 to +6.0c. That falls just short of the bar.
On the sales the paper account actually filled, the test could not run properly: with only
4 calibration closes it kept the current rule. Nothing changes in live trading. The engine
now logs its fair value at every long-shot firing so the test can run forward. The rule and
the evaluation date are fixed below.

Code: `src/edgebook/research/longshot_fair_value.py` (tests in
`tests/test_longshot_fair_value.py`). Reproduce:

```bash
uv run python -m edgebook.research.longshot_fair_value reports/fly/2026-10-17.jsonl reports/fly/2026-10-18.jsonl --account reports/fly/paper-longshot.json --results reports/fly/kalshi-results.json
```

## What was tested

The paper rule (`engine.maker.longshot_quote`) sells a 2-15c long shot 15-60 min before
close by resting a bid on the favourite at 1 − long-shot ask. It skips the sale when fair
value says the long shot is worth at least its ask. Fair value here means 20% BRTI model
blended into Kalshi's mid in log-odds. So the current rule **already uses fair value, with a
margin of zero**. It sells when

    edge = long-shot ask − fair value of the long shot  (= favourite fair value − price paid)

is above 0. The question is whether demanding a larger edge `m` picks better sales.

How small the edge usually is: across the 24,273 settled logged firings, edge percentiles
are p5 −0.97c, p25 −0.21c, median +0.15c, p75 +0.41c, p95 +0.85c. The 20% model weight moves
the mid by under a cent nearly all the time. Most of the edge is half the spread, because the
ask sits above the mid.

## Data

- Engine decision logs, `/data/engine/decisions/2026-10-17.jsonl` and `2026-10-18.jsonl` on
  `edgebook-recorder`, downloaded read-only to `reports/fly/`. They run from 06:19 UTC Oct 17
  to 12:50 UTC Oct 18. That is all the logs that exist: the volume was recreated on Oct 16.
  Rows come every 10 s, but only for the **primary** strategy's markets: 15-minute markets
  plus the 6 hourly strikes nearest the index. Only hourly above/below (KXBTCD) rows fall in
  the long-shot band with 15-60 min left. Far strikes and range markets (KXBTC), where the
  paper account also trades, are missing.
- The long-shot strategy's fair value is recomputed from the logged `model_up` and
  `market_up` with `blend_fair(·, ·, 0.2)`. That strategy does not take the fitted weight.
- Paper account `/data/engine/paper-longshot.json` (started 22:01 UTC Oct 17).
- Official results: Kalshi public `GET /markets/{ticker}` (296 settled tickers).
- Data checks (stale index and similar) can't be separated in the old log, which records only
  an all-gates flag, so they are not applied to it. The new log records them.

## Pre-registration (written before any results were joined)

- Firing: UP ask in [2c, 15c], else 1 − UP bid in band; 900 ≤ seconds left ≤ 3600.
- Policy(m): one entry per market, at the earliest logged firing with edge > m. The P&L is
  `seller_pnl(long ask, result)` per contract, including the maker fee. Grid m ∈ {0, 0.25,
  0.5, 0.75, 1.0}c. m = 0 is the current rule. "Band only" means no fair-value check.
- Calibration: settled closes sorted by close time. The earliest third picks the m with the
  best mean P&L, needing at least 10 markets; ties go to the smaller m.
- Evaluation, run once on the later two thirds: policy(m*) vs the current rule, as the
  difference in mean per-contract P&L with a 95% interval resampling whole closes (2,000
  draws).
- Paper-filled subset: markets the paper account bought. The edge is taken from the latest
  same-side logged firing in the 10 minutes before the first fill. The P&L is the account's
  own settle P&L per contract.
- Verdict: YES if the interval is entirely above 0. NO if it is entirely below 0, or if the
  point estimate is ≤ 0 with at least 30 evaluation closes. Otherwise NOT YET.

## Results (bankroll-bar format)

Effect is the mean P&L per contract, with a 95% interval from resampling closes. Trades are
one per market. The worst close is the summed 1-contract P&L of the worst hour.

**All logged firings.** Calibration: 10 closes, Oct 17 03:00-12:00 ET. Evaluation: 20
closes, Oct 17 13:00 to Oct 18 08:00 ET. m* = **0.5c**.

| evaluation (20 closes) | trades | per contract | 95% interval | trades/day | worst close |
|---|---|---|---|---|---|
| band only (no fair check) | 151 | −1.52c | −6.96 to +4.08c | 181 | −320c (Oct 17 18:00) |
| current rule (edge > 0) | 138 | −0.54c | −5.58 to +4.63c | 166 | −227c (Oct 17 18:00) |
| **edge > 0.5c** | 108 | **+2.33c** | **−0.83 to +5.80c** | 130 | −107c (Oct 17 18:00) |

- 0.5c rule minus current: **+2.87c** (−0.14 to +5.98c) → pre-registered verdict
  **NOT YET**.
- Current minus band only: +0.98c (−0.97 to +3.02c). Today's zero-margin check adds nothing
  measurable.
- Calibration means (trades): 0c +2.00 (52), 0.25c +1.09 (45), 0.5c +3.24 (25), 0.75c
  +4.00 (8; below the 10-market floor), 1.0c none.
- Robustness (not part of the decision): the gain rises steadily with m on the evaluation
  closes (0.25c +0.1, 0.5c +2.9, 0.75c +4.3, 1.0c +5.5c). Dropping any single close leaves
  +1.8 to +3.2c. Most of it comes from two bad hours: 18:00 Oct 17 (current rule −227c on
  14 strikes, the 0.5c rule −107c on 11) and 06:00 Oct 18 (−218c on 11 vs −31c on 10).
- **Correction.** The first run of the evaluation had a bug: one close could land on both
  sides of the split, because close times were rounded relative to the log timestamp. That
  run picked 0.75c and reported a pass (+4.3c, +0.8 to +7.5c). The bug is fixed (and covered
  by a test). The table above is the corrected result, and the verdict is the corrected one.

**Paper-filled sales.** 60 trades matched to a logged firing. Calibration: 4 closes;
evaluation: 9 closes. Only m = 0 kept at least 10 calibration markets, so m* = **0**, the
current rule. The evaluation is the current rule against itself (difference 0, NOT YET).

| evaluation (9 closes) | trades | per contract | 95% interval | trades/day | worst close |
|---|---|---|---|---|---|
| band only | 47 | −0.21c | −8.94 to +6.94c | 125 | −155c (Oct 18 06:00) |
| current rule (= m*) | 44 | −0.74c | −9.82 to +6.85c | 117 | −155c (Oct 18 06:00) |

- The earlier lead (fair value above price at fill, +4.98c on 41 trades) used the fill
  reason's fair value. That value is rounded to a cent and fixed when the order was placed,
  so it is not a clean measure of the edge either.

## Why "not yet" rather than "yes"

1. **The interval touches zero.** The all-firings test missed the pre-registered bar.
2. **Fills.** "All firings" prices every firing at the ask as if it filled. The gap study
   (`docs/LONGSHOT_GAPS.md`) found that filled long-shot sales are adversely selected: they
   lose about 1c where unfilled quotes look profitable. The fills test shows no gain, though
   it is far too small to tell either way.
3. **Thirty hours, one regime.** The 20 evaluation closes are consecutive hours of one day.
   Resampling closes treats them as independent, but BTC's path ties them together. The gain
   is avoiding two bad hours, and a different day could have different bad hours.
4. **Coverage.** Only the 6 strikes nearest the index were logged. The far strikes and range
   markets the paper account sells were not.

What would change it: the forward test below.

## Forward test (pre-registered now)

A separate commit on this branch makes the paper engine write one row per long-shot firing
every 10 s, to `/data/engine/decisions/longshot/<day>.jsonl` (`engine.loop.longshot_log_rows`).
Rows are written whether or not the current rule sells and whether or not the order fills.
Each row has the long-shot side and ask, the strategy's fair value and edge, `model_up`,
`market_up`, the model weight, whether the current rule fires, the resting price, contracts
held and the data checks. It covers every strike and series the strategy evaluates. The
research module reads these rows directly (`firing_from_row` uses the logged fair value,
when the data checks passed). It changes nothing the strategy does, and the live pilot is
untouched.

- **Rule (fixed, no recalibration):** sell only when edge > **0.5c**, the m chosen above on
  the all-firings sample (the paper sample was too small to choose one).
- **Primary test:** paper-filled sales, 0.5c rule minus current rule. Per-contract P&L,
  with a 95% interval resampling closes, plus trades/day and worst close.
- **Secondary:** all logged firings, same comparison.
- **When:** after 14 full days of `decisions/longshot/` logs, counted from the first day the
  change is deployed (2026-11-01 if deployed on 2026-10-18), and only once there are at least
  **300 paper-filled markets over at least 100 closes**. If the count is short at 14 days,
  extend in 7-day steps.
- **Pass:** paper-filled interval entirely above 0 → adopt the margin in the paper strategy
  first, then the pilot. Interval entirely below 0, or point ≤ 0 → drop the idea. Otherwise
  report NOT YET with the numbers.
