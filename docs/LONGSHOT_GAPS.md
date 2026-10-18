# Long-shot seller gap analysis — 2026-10-18

## Question

The long-shot seller (paper strategy `longshot`, and the real-money pilot that copies it)
rests a bid on the favourite whenever one side is offered at 2–15¢ with 5–60 minutes left,
then holds to the result. Where does it lose, and can a model find sales to skip?

Code: `src/edgebook/research/longshot_gaps.py` (dataset, models, report) and
`scripts/longshot_restricted.py` (rule variants). Outputs: `reports/longshot-gaps.json`,
`reports/longshot-rule-variants.json` (git-ignored; regenerate with `uv run python -m edgebook.research.longshot_gaps` and `uv run python scripts/longshot_restricted.py`, about 10 s). A second run reproduced the first byte for byte.

## Data and method

- 30 days of 1-minute Kalshi quotes, Sep 16 – Oct 16, 2026: 2,840 fifteen-minute markets
  and 9,445 hourly strikes, labelled with Kalshi's official results. Coinbase 1-minute spot
  for features only.
- Every 5 minutes from 60 to 5 minutes before close, the paper rule was replayed. That gives
  48,671 firings across 10,078 markets and 2,108 closes.
- **Fill proxy:** a sale counts as filled if, within 10 minutes, a trade printed at or
  through our price, or the opposite quote reached it. Real fills join the back of the
  queue, so actual fills are probably *more* adverse than this proxy.
- Features at decision time: series, minutes left, price, spread, side, distance to the
  strike in volatility units, a driftless volatility-model probability, 5- and 15-minute
  momentum toward the long shot, 60-minute volatility and its ratio to the daily level,
  the 5-minute price change, 15-minute volume, hour, and weekend.
- **Chronological split by close:** train on the first 60% (1,264 closes), validate on the
  next 20% (422), test once on the last 20% (422). Each policy takes one entry per market
  held to the result, like the strategy. 95% intervals resample whole closes.

## Finding 1 — the edge lives in orders that never fill (adverse selection)

| Firings | Count | Long shot won | Seller ¢/contract | 95% interval |
|---|---:|---:|---:|---:|
| All | 48,671 | 4.3% | +0.43 | −0.27 to +1.09 |
| Would have filled | 33,101 | 5.9% | **−1.06** | −1.97 to −0.21 |
| Would not have filled | 15,570 | 1.0% | +3.61 | +3.30 to +3.89 |

Long shots *quoted* at 2–15¢ are overpriced; that is the earlier `research.longshot`
result, and it holds here. But a resting sale gets taken mostly when someone has reason to
buy the long shot. Filled long shots won six times as often as unfilled ones. The
"+1 to +4¢" in the strategy's docstring was measured on quotes, not on fills.

## Finding 2 — the final 15 minutes lose in every period

Filled firings, earlier closes (train + validation) vs untouched later closes (test):

| Condition | Earlier ¢/contract (95% interval) | Later ¢/contract (95% interval) |
|---|---:|---:|
| 5–15 min left | −4.00 (−5.56 to −2.58) | −6.33 (−10.17 to −2.88) |
| 15–30 min left | −0.57 (−1.82 to +0.51) | −2.71 (−5.06 to −0.64) |
| 30–45 min left | −0.14 (−1.35 to +0.98) | −2.91 (−5.56 to −0.39) |
| 45–60 min left | −0.46 (−1.70 to +0.67) | −1.78 (−4.41 to +0.75) |
| 15-minute series (all ≤15 min left) | −8.08 (−11.31 to −5.11) | −6.07 (−12.43 to −0.61) |

The 15-minute series' filled long shots won 14–16% of the time at an average price near 9¢.
Every 15-minute-market sale falls inside the final 15 minutes, so dropping that window also
drops the whole series.

Other splits (price band, distance to strike, momentum, volatility regime, recent price
change) were not consistent between the two periods.

## Finding 3 — ML does not beat Kalshi's price

Test-set probability that the long shot wins (lower is better):

| Forecast | Brier | Log loss |
|---|---:|---:|
| Kalshi long-shot ask (market) | **0.0513** | **0.2018** |
| Logistic regression, isotonic-calibrated on validation | 0.0517 | 0.2059 |
| Gradient-boosted trees, isotonic-calibrated on validation | 0.0525 | 0.2111 |

Each model's skip cut-off was chosen on validation only and applied once to test. Filtering
made results slightly worse:

| Test policy (one entry per market, filled) | Markets | ¢/contract | 95% interval |
|---|---:|---:|---:|
| Sell every firing | 1,640 | −2.42 | −4.39 to −0.58 |
| Current paper rule | 1,336 | −2.49 | −4.60 to −0.56 |
| Gradient-boosted filter | 1,396 | −2.71 | −4.87 to −0.58 |
| Logistic filter | 504 | −3.86 | −7.47 to −0.51 |

This agrees with the 2026-10-17 diagnostic: these features add nothing the market price
does not already contain.

## Rule variants (one entry per market, filled)

| Rule | Earlier ¢ (95% interval) | Later ¢ (95% interval) |
|---|---:|---:|
| Current (5–60 min, both series) | −1.07 (−2.14 to −0.09) | −2.42 (−4.39 to −0.58) |
| Hourly only | −0.52 (−1.67 to +0.55) | −2.01 (−4.02 to +0.01) |
| **15–60 min left** (= hourly only, 15+ min) | −0.44 (−1.63 to +0.63) | −1.89 (−3.97 to +0.17) |
| 15–60 min, ask ≤6¢ | −0.50 (−1.58 to +0.50) | −1.29 (−3.03 to +0.38) |
| 15–60 min, distance ≥1.5σ | −0.49 (−1.67 to +0.61) | −1.64 (−3.60 to +0.25) |

## Change made

`LongshotConfig.min_secs_left` goes from 300 to 900. The paper strategy and the real-money
pilot stop selling 15 minutes before close, and the pilot's Kalshi order expiry
(`EDGEBOOK_EXECUTION_LONGSHOT_EXPIRE_S`) defaults to 900 to match. This is the only cut that
was consistent across both periods. It roughly halves the loss; it does **not** make the
strategy profitable: every remaining variant is break-even to negative.

## What would actually move this

1. **Real fills, not a proxy.** The recorder on Fly keeps trade prints with taker side and
   full books. Re-running Finding 1 on recorded queue position would show how much worse
   real fills are, and whether specific order-flow patterns precede adverse fills.
2. **Cancel before adverse fills.** The pilot only makes money on orders that don't fill
   against informed buyers. The testable idea: cancel when the long shot's book or tape
   turns (bids stepping up, buyer-initiated prints). That needs the recorded books above.
3. **More independent days.** 30 days and 422 test closes give intervals of ±2¢; the
   effects worth finding are smaller than that.

Until one of these shows a positive, untouched-test result, expect the live pilot to lose
roughly 0.5–2¢ per filled contract; its money caps bound the damage.
