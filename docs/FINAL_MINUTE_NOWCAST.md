# Final-minute nowcast: 2026-10-18

## Question

Kalshi BTC contracts (KXBTC15M, KXBTCD) settle on the simple average of the BRTI prints
timed in `[close - 60s, close)`. Inside that window part of the average is already known.
Does exact knowledge of the partial average, plus a forecast of the rest, beat Kalshi's
executable prices after the taker fee? If so, how fast must the order path be to keep it?

Code: `src/edgebook/research/final_minute.py`. Output: `reports/final_minute.json`
(git-ignored). Regenerate with

```
uv run python -m edgebook.research.final_minute <recorder snapshot root> --out reports/final_minute.json
```

## Verdict

**No edge. Do not trade it.** At zero delay the strategy loses 12.1c per contract (95%
interval -23.5c to -0.9c, resampling closes). No delay from 0 to 2000 ms has a positive
mean, so there is **no break-even latency**: a faster order path cannot rescue it. Kalshi's
mid already prices the final minute better than the nowcast does at every checkpoint. The
partial average is not private information: Kalshi broadcasts every BRTI print to everyone
about 58 ms after it is timed, and the market prices it.

It fails the bankroll bar on every count: the effect is negative with an interval that
excludes zero at the realistic delay, and the threshold that looked best on the training
closes already lost money there.

## Data

- Fly recorder snapshot `data/recorder-snapshot-2026-10-18/raw`: BRTI prints from
  2026-10-16 05:48 UTC to 2026-10-18 12:50 UTC (196,384 prints), Kalshi books and official
  settlements.
- 10,125 settled single-strike contracts in 219 closes (with at least 15 minutes of BRTI
  history before the close, for the volatility estimate).
- **Settlement check:** recomputing each result from the recorded window agrees with
  Kalshi on **10,123 of 10,123** contracts (2 closes lacked a full window). The largest gap
  between our average and Kalshi's expiration value is $0.19 of BTC. The settlement rule
  is reproduced exactly, so the losses below are not a settlement-model error.

## Method

- **Clock.** Everything runs on the recorder's receipt clock. A decision at `D` uses only
  the BRTI prints and book updates the recorder had received by `D`.
- **Nowcast** at 60, 45, 30, 15 and 5 s before close: the mean of the window prints
  received so far, plus the remaining prints modeled as a driftless random walk from the
  latest print, with per-second volatility from the previous 15 minutes of 1 s prints
  (floored). The final average is then normal with an exactly computed variance.
- **Entry.** Per market and checkpoint, pick the side with the larger nowcast edge after
  the taker fee, `fair - ask - 0.07 p (1 - p)`. Take one attempt per market, at the
  earliest checkpoint whose edge clears the threshold. Order: buy at the best ask with the
  ask as the limit, up to 25 contracts or the size shown at the limit.
- **Delay.** The order arrives `d` ms after the decision (0, 100, 300, 500, 1000,
  2000) and walks the book recorded at `D + d` up to its original limit, with partial
  fills from depth and Kalshi's taker fee rounded up per fill level. Hold to the official
  result.
- **Split.** The threshold is chosen on the earliest third of closes (72) as the one
  with the largest total P&L among those with at least 20 fills, then frozen. All results
  below are on the later two thirds (145 closes, 1.51 days) only.

## Result 1: Kalshi's mid beats the nowcast

Contested markets only (mid between 3c and 97c), all closes. Lower Brier is better.

| Seconds left | Markets | Brier, nowcast | Brier, Kalshi mid | Spread | Value of buying the nowcast's side at the ask |
|---:|---:|---:|---:|---:|---:|
| 60 | 86 | 0.168 | **0.117** | 1.1c | -7.3c (78 markets) |
| 45 | 57 | 0.128 | **0.107** | 1.3c | -1.5c (48) |
| 30 | 35 | 0.111 | **0.094** | 1.2c | -2.0c (30) |
| 15 | 10 | 0.104 | **0.096** | 1.3c | -3.5c (8) |
| 5 | 1 | 0.805 | 0.931 | 0.6c | one market, not informative |

The gap narrows as the close approaches, but the nowcast never catches up. Where it
disagrees with Kalshi, Kalshi is usually right, so selecting trades by disagreement
selects the nowcast's mistakes. Very few markets are still contested in the last 15 s:
the market has converged by then.

## Result 2: the delay curve (test closes only)

Threshold fixed at **5c** on the training closes. On those closes, every threshold
with at least 20 fills lost money (5c: -$0.67 over 20 fills); only thresholds with 15
fills or fewer were positive, which is noise at that size.

Bankroll-bar format: effect per contract with a 95% interval resampling closes, trades
per day, and the worst single close (sum of per-contract P&L in that close).

| Delay | Attempts | Filled | Fill rate | Effect per contract | 95% interval | Trades/day | Worst close |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 ms | 43 | 43 | 100% | **-12.1c** | -23.5c to -0.9c | 28.5 | -$1.05 |
| 100 ms | 43 | 32 | 74% | -9.2c | -19.7c to +2.2c | 21.2 | -$0.72 |
| 300 ms | 43 | 29 | 67% | -11.7c | -23.2c to +0.1c | 19.2 | -$0.71 |
| 500 ms | 43 | 28 | 65% | -10.1c | -20.6c to +1.9c | 18.5 | -$0.71 |
| 1000 ms | 43 | 24 | 56% | -6.0c | -16.8c to +5.8c | 15.9 | -$0.60 |
| 2000 ms | 43 | 21 | 49% | -6.2c | -16.1c to +6.3c | 13.9 | -$0.46 |

By series at 0 ms: KXBTC15M -13.9c (-27.4c to +0.5c, 21.8 trades/day, 33 fills);
KXBTCD -6.1c (-23.1c to +17.6c, 6.6 trades/day, 10 fills).

Re-choosing the threshold on the training closes for each delay does not help: every
delay keeps a negative mean (0 ms -12.1c, 100 ms -7.6c, 300 ms -4.7c, 500 ms -4.7c,
1000 ms -3.3c, 2000 ms -0.5c), and the interval excludes zero at 0 to 500 ms.

The mean gets less negative with longer delays only because fewer orders fill (fill rate
falls from 100% to 49%). The intervals overlap heavily, so this is not evidence that
slower is better. There is no delay at which the curve crosses zero.

**Break-even latency: none.** Even an instantaneous order loses.

## Result 3: latency of the current Fly iad recorder

Measured from the recorder's own timestamps, in ms:

| Hop | p10 | p50 | p90 | p99 |
|---|---:|---:|---:|---:|
| BRTI print time to Kalshi receipt | 29 | 58 | 83 | 104 |
| Kalshi receipt to recorder (BRTI) | 9 | 11 | 152 | 9,021 |
| BRTI print time to recorder | 43 | 75 | 210 | 9,085 |
| Kalshi book send to recorder | 6 | 8 | 170 | 9,270 |

- From Fly iad, Kalshi's feed arrives in about **10 ms** at the median and **150 to 170
  ms** at p90. The p99 of about 9 s is stalls and reconnects.
- In this study the delay `d` runs from when the recorder receives the data to when the
  order reaches the book. If the order hop is about as fast as the feed hop, the
  **realistic delay of the current setup is roughly 10 to 50 ms at the median and about
  200 ms at p90**, before any compute time. That puts it in the 0 to 100 ms rows above,
  which lose 9 to 12c per contract. (The order-path latency itself was not measured;
  this is inferred from the feed hops.)
- Every participant gets BRTI through Kalshi about 58 ms after the print time, so being
  fast buys no information others lack. Being fast only helps if the model is better than
  the market, and it is not.

## Caveats

- **Small sample.** 145 test closes over 1.5 days, 43 entries. The conclusion holds
  anyway: the training third shows the same losses, and Kalshi beats the nowcast on
  Brier at every checkpoint across all 219 closes.
- **The nowcast is overconfident.** Its Brier score is worse than the mid. A
  better-calibrated nowcast (fatter tails via `--nu`, or volatility scaled to the final
  minute) would mostly agree with Kalshi, which leaves even fewer edges after the 1c to
  2c spread and fee. Better calibration would shrink the losses, not create an edge.
- Fills assume we take all displayed size at the limit, ahead of anyone else who reacts to
  the same print. If anything, this overstates the strategy.
- This contradicts the earlier +4.41c at the recorded quote in
  `research/2026-10-17-btc-edge`. That result used different signals on a smaller,
  same-day sample, and already fell to -0.57c after 300 ms.

## What to do next

Stop pursuing a final-minute taker strategy on the settlement average. If the final
minute is revisited, the only angle this data leaves open is providing liquidity (resting
quotes that get paid the spread), not taking it. That needs its own fill model.
