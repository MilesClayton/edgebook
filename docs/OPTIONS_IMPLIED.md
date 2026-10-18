# Options-implied odds vs Kalshi's hourly ladder — 2026-10-18

## Verdict

**No edge. Do not use options-implied probabilities to size up.** The pre-registered rule,
evaluated once on 467 later closes, does not pass the bankroll bar:

| Bankroll bar (test: Oct 1 08:00 – Oct 16 UTC, 467 closes, 20.0 days) | Value |
|---|---:|
| Effect per contract (100-lot fills, Kalshi taker fee) | **−1.38¢** |
| 95% interval, resampling closes | **−3.03¢ to +0.20¢** |
| Same with 1-contract fee rounding (worst case) | −1.88¢ (−3.54 to −0.31) |
| Trades per day (one entry per market) | 166 |
| Worst close (one contract per trade) | −$8.94 |
| Best close | +$4.65 |
| Closes traded | 449 of 467 |
| Win rate | 31% |

Kalshi's own mid was the best probability forecast in both periods, ahead of every options
view. Options vol and Kalshi's ladder vol agree on average. Where they disagree, Kalshi is
closer to what happens.

## Question

Kalshi's hourly `KXBTCD` event lists a ladder of "BTC above K at the top of the hour"
strikes, settled on the 60-second average of the CF Benchmarks BRTI. BTC options on Deribit
are a separate market with its own price discovery. Earlier studies built features from
Coinbase spot (momentum, realized vol, distance to strike) and found they add nothing to
Kalshi's price (`reports/ML_DIAGNOSTIC_2026-10-17.md`, `docs/LONGSHOT_GAPS.md`). Do options
imply settlement odds that disagree with the ladder in a way that survives Kalshi's fees?

## Data that was available

| Source | What | Access |
|---|---|---|
| Deribit `public/get_volatility_index_data` | DVOL (30-day BTC implied vol) as 1-minute candles, full history | public, no key |
| `history.deribit.com` `public/get_last_trades_by_currency_and_time` | every BTC option trade, including expired daily options, each with Deribit's IV at the fill, the mark price and the Deribit index | public, no key |
| Kalshi backfill (`research/2026-10-17-btc-edge/history/`) | 9,445 hourly strikes, 700 closes, 1-minute bid/ask, official results, Sep 16 – Oct 16 | existing |
| Coinbase 1-minute BTC-USD | spot proxy for BRTI | existing |

Short-dated options exist: Deribit lists a **daily expiry at 08:00 UTC**, so the front
expiry is always 1–24 hours away. The fetch covered Sep 15 – Oct 17: 216,131 option trades
and 46,007 DVOL minutes, in about two minutes (`uv run python -m edgebook.research.deribit
--dir history --start 2026-09-15 --end 2026-10-17`; re-running resumes). Because history was
available, no forward recorder was needed. To extend the sample, re-run the fetch and the
Kalshi/Coinbase backfills with a later end date.

**Terms.** Deribit's terms limit market data to personal use and forbid redistribution
without approval. The raw data stays in the git-ignored `history/` folder. This document
publishes aggregate statistics only.

## Method

Code: `src/edgebook/research/deribit.py` (fetch) and `src/edgebook/research/options_implied.py`
(model, comparison, rule, report). Tests: `tests/test_options_implied.py`.

- **Decision times:** every 5 minutes from 60 to 15 minutes before each close. A Kalshi
  candle, spot minute, DVOL minute or option trade is used only if it ended at or before the
  decision time.
- **Options vol, three variants:**
  - `opt`: median ATM IV of the front daily expiry (at least 1 h to expiry), inverted from
    the **mark price** of trades within ±2% of the index in the previous 30 minutes (at least
    3 trades; otherwise the next expiry). Available at 98.6% of decision times.
  - `opt_seasonal`: `opt` rescaled by intraday seasonality. The ratio is
    sqrt(mean variance in the hour to the Kalshi close / mean variance until the option's
    expiry), with hour-of-day variance weights from Coinbase 1-minute returns **in the train
    period only**. This is the term-structure adjustment: a daily option's IV averages quiet
    Asian hours with the busy US open, and a single Kalshi hour is one or the other.
  - `dvol`: Deribit's DVOL index (30-day horizon), unadjusted.
- **Probability:** driftless lognormal P(S_T > K) with Coinbase spot. The variance time is
  shortened by 40 seconds because the settlement averages the final minute, which has a
  third of a point price's variance.
- **Kalshi's ladder:** at each event and decision time, an OLS of probit(mid) on ln(K/S)
  across the tradeable strikes (mid 3–97¢, spread ≤ 10¢, at least 3 strikes). The slope gives
  Kalshi's implied vol; the intercept gives its **level shift** in standard deviations from
  driftless spot.
- **Vol vs level disagreement:** `vol_swap` keeps Kalshi's centre and swaps in the options
  vol. `level_swap` keeps Kalshi's vol and centres on spot. If either beat the fitted ladder,
  that component would be where options (or spot) know something Kalshi doesn't.
- **Basis:** BRTI settle vs Coinbase at close: +0.19 bps mean, 2.4 bps sd (700 closes).
  Deribit index vs Coinbase (index print within 60 s): +0.14 bps mean, 2.4 bps sd. The typical
  one-hour BTC move was 36 bps, so the index choice is noise at this horizon.
- **Split:** the earliest third of closes (233, Sep 16 – Oct 1 08:00 UTC) for exploration
  and rule selection; the later two thirds (467 closes) evaluated once.

## Comparison: who forecasts settlement better?

Brier score / log loss over every (market, decision time) where all forecasts exist (lower is
better):

| Forecast | Train Brier | Train log loss | Test Brier | Test log loss |
|---|---:|---:|---:|---:|
| **Kalshi mid** | **0.1117** | **0.3611** | **0.1232** | **0.3934** |
| Kalshi ladder fit (2 numbers) | 0.1149 | 0.3703 | 0.1239 | 0.3955 |
| Level swap (spot centre, Kalshi vol) | 0.1155 | 0.3715 | 0.1233 | 0.3940 |
| Vol swap (Kalshi centre, options vol) | 0.1150 | 0.3705 | 0.1258 | 0.4033 |
| Options `opt_seasonal` | 0.1153 | 0.3709 | 0.1250 | 0.4010 |
| Options `opt` | 0.1151 | 0.3768 | 0.1260 | 0.4139 |
| Options `dvol` | 0.1174 | 0.3830 | 0.1309 | 0.4268 |

(18,460 train rows across 2,018 event snapshots; 29,106 test rows across 3,725.)

- **Vol disagreement.** The log ratio of seasonal options vol to Kalshi's ladder vol had mean
  +0.00 on train and +0.11 on test, with sd 0.34–0.37 per snapshot: the two agree on average
  and scatter widely. Swapping options vol into Kalshi's ladder made the forecast **worse** in
  both periods, so in the scatter Kalshi is closer to what happens.
- **Level disagreement.** Kalshi centres its ladder +0.02 to +0.04 sd above driftless spot
  (sd 0.07). Recentring on spot was slightly worse than the fit itself. There is no level
  information to take.
- **Realized width.** The std of ln(settle / spot) / (σ√τ) should be 1.0 for a vol of the
  right size:

  | Vol source | Train | Test |
  |---|---:|---:|
  | Kalshi ladder | 1.06 | 1.07 |
  | `opt_seasonal` | 1.04 | 0.92 |
  | `opt` (raw front-expiry IV) | 1.24 | 1.06 |
  | `dvol` | 1.09 | 0.84 |

  Raw front IV understated the Kalshi hour on train (24% too narrow), because Kalshi's busiest
  hours are the high-variance ones. The seasonal adjustment fixes the average, but it is still
  no better than Kalshi, which is consistently slightly narrow (6–7%). DVOL is a 30-day
  number and swings between too narrow and too wide.

## Trading test

### Pre-registration (committed in `ca43bf4` before any test close was evaluated)

`select_rule` searched 36 combinations on train only: vol source (`opt`, `opt_seasonal`,
`dvol`) × minimum edge after fee (0, 1, 2, 3, 5, 8¢) × role (taker at the ask, or maker at
the bid with a 10-minute fill proxy as in `LONGSHOT_GAPS.md`). Each took one entry per
market, at the first decision time the edge cleared. **All 36 lost on train.** The rule that
lost least was registered:

- `dvol`, buy the side the options probability says is underpriced by **≥ 5¢ after the
  Kalshi taker fee**; train −1.13¢/contract (95% interval −3.77 to +0.90), 1,638 trades.

Fees are Kalshi's quadratic schedule, 0.07·P·(1−P) for takers and 0.0175 for makers. The
headline uses a 100-contract fill (Kalshi rounds each fill up to a cent once). The table at
the top also shows 1-contract rounding.

### Result

The verdict table is at the top. By decision time on test, every horizon except 25 and 30
minutes lost; those two were positive on 164 and 150 trades, within noise. Most entries
(1,797) fired at 55 minutes. Hourly markets open exactly 60 minutes before close, so at the
60-minute decision time most have no finished 1-minute candle yet.

### Post hoc (not registered, for context only)

The same 5¢ threshold on the later closes for every vol source and role:

| Vol source | Role | Trades | ¢/contract | 95% interval | Trades/day | Worst close |
|---|---|---:|---:|---:|---:|---:|
| `opt` | taker | 2,554 | −1.15 | −3.11 to +0.62 | 128 | −$8.01 |
| `opt` | maker | 3,048 | −1.40 | −3.27 to +0.24 | 153 | −$9.85 |
| `opt_seasonal` | taker | 2,092 | −0.02 | −2.20 to +1.86 | 105 | −$4.25 |
| `opt_seasonal` | maker | 2,666 | −0.16 | −1.83 to +1.44 | 134 | −$7.16 |
| `dvol` (registered) | taker | 3,309 | −1.38 | −3.16 to +0.15 | 166 | −$8.94 |
| `dvol` | maker | 3,677 | −1.93 | −3.51 to −0.43 | 184 | −$9.85 |

The best of these, seasonal options vol, is break-even on test after losing 2.96¢ on train
(taker, 5¢). That is not an edge. It is consistent with options knowing about as much as
Kalshi, with the gap eaten by the spread and the fee.

## Why it fails, and what would change it

1. **Kalshi already prices the vol.** Market makers on the ladder quote off the same
   information the options market has, plus Kalshi-specific order flow. A 30-minute-old ATM
   IV is a slower signal than a live ladder.
2. **The disagreements are mostly noise in the options estimate.** Trade-based IV on a
   1–24 hour expiry moves with each print. Most large "edges" are those prints, not
   mispricing.
3. **Fees and spread.** A taker pays roughly 1.5–2¢ in fee plus half the spread on a 50¢
   contract. A forecast that is merely as good as the mid loses that every time.

Things that could still help, none tested here:

- **Live Deribit book IV instead of trade IV.** A per-second mark IV from the order book
  removes print noise. Deribit's `public/ticker` provides it. Testing this needs a forward
  recorder, because historical book IV is not public.
- **Smile-consistent tails.** The flat-vol lognormal ignores the daily smile. The ladder's
  extreme strikes are where Kalshi's long-shot bias lives (`LONGSHOT_GAPS.md`), and the options
  smile might price those tails better than either flat vol. This needs an hourly-horizon smile
  fit, not a daily one.
- **More closes.** Twenty test days give intervals of about ±1.6¢. An edge under 1¢ per
  contract would not show up here.

## Reproduce

```bash
uv run python -m edgebook.research.deribit --dir history --start 2026-09-15 --end 2026-10-17
uv run python -m edgebook.research.options_implied \
  --kalshi-dir research/2026-10-17-btc-edge/history --deribit-dir history --train-only
uv run python -m edgebook.research.options_implied \
  --kalshi-dir research/2026-10-17-btc-edge/history --deribit-dir history --rule dvol:0.05:taker
```

Outputs go to `reports/options-implied*.json` (git-ignored). A full run takes about 25 s.
