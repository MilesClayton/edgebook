# Range vs ladder: are KXBTC ranges priced consistently with KXBTCD?

Date: 2026-10-18. Code: `src/edgebook/research/range_ladder.py` (tests in
`tests/test_range_ladder.py`). Offline research only: no orders, no live changes.

## Answer

**No fee-surviving gap worth trading on 1-minute candles.** Across 704 hourly closes (30
days), buying a range and selling its two-strike replica (or the reverse) at the
quoted prices beat Kalshi's taker fee on every leg in 0.33% of range-minutes. The typical
gap was 0.2 to 0.5c per contract. Leaving out one anomalous close, taking one contract
at every gap-minute would have earned **0.67c per close (95% CI 0.59 to 0.76c)**. That is an
upper bound: candles carry no depth, the three legs are only aligned to the minute, and
a real taker has to cross three books one after another.

The depth-backed, synchronized test needs the recorder's books, which this run could not
reach (see "Still open"). The module is ready for them, and the candle result says to
expect nothing durable there either.

**The paper long-shot seller's KXBTC losses were not range mispricing.** In the band it
trades (range YES ask 2 to 15c, 15 to 60 minutes before close), the range YES ask sat
**1.4c above** the ladder-implied value (CI 1.41 to 1.47c), and the ladder value was
itself 0.5c above the realized hit rate. Selling those long shots was selling something
priced rich on both counts. The -19.6c over 33 trades has to come from which orders
filled (the adverse selection documented in `docs/LONGSHOT_GAPS.md`) and from a small
sample, not from ranges being cheap against the ladder.

## Setup

**Contracts.** Each hourly close lists 183 KXBTC markets (181 ranges $100 wide plus two
open tails) and 183 KXBTCD strikes. Both settle on the same number quoted to the cent.

| KXBTC market | edges (`floor`, `cap`) | pays the same as |
|---|---|---|
| range `B81050` | 81000.00, 81099.99 | above(80999.99) minus above(81099.99) |
| top tail `T92800` | floor 92799.99 | above(92799.99), the identical contract |
| bottom tail `T74200` | cap 74200.00 | 1 minus above(74199.99) |

So a range floor of A maps to the ladder strike A minus 1 cent, and a range cap of
B (already ...99.99) maps to the strike B. **Every one of the 128,962 ranges matched
both of its ladder legs at the same close, and on all 128,962 settled markets the
ladder-implied result equals Kalshi's official result (0 mismatches)**, which confirms
the boundary convention.

**Baskets.** Kalshi only lets you buy YES or NO, so each inconsistency is a basket that
pays the same in every outcome:

- `buy_range`: range YES + lower-strike NO + upper-strike YES pays exactly $1;
- `sell_range`: range NO + lower-strike YES + upper-strike NO pays exactly $2 ($1 for a
  tail, which has one ladder leg).

A gap is a moment when the basket's asks plus the taker fee on every leg
(`ceil(0.07 * C * p * (1 - p))` cents per leg, `core.fees`) cost less than the payoff.

## Data

1. **Recorder (synchronized books).** The Fly recorder already records KXBTC:
   `Settings.record_series` defaults to `("KXBTCD", "KXBTC", "KXBTC15M")` and every
   discovered ticker gets `orderbook_delta` and `trade` subscriptions. No recorder
   change is needed.
2. **Kalshi 1-minute candles (this run).** The public `/markets/candlesticks` endpoint
   backfills both series. The repo's existing backfill keeps only "tradeable" strikes,
   which leaves about 7 ladder strikes per hour, too few to replicate most ranges, so
   `history.kalshi.backfill` gained `keep_all=True`. 30 days, 704 closes, 128,962
   markets per series, 1.13M range candles and 3.43M ladder candles.

Candles are not synchronized books: each leg's bid and ask is its quote at the end of
the minute, with no size. A minute counts only if all three legs have a two-sided,
uncrossed candle. That restricts the scan to near-money ranges (278,917 range-minutes,
about 396 per close); a deep range with a zero bid on a leg is not scanned. The last
minute before close is excluded. Fees are per contract on a 100-lot.

## Results (candles, 704 closes)

| | buy_range | sell_range |
|---|---|---|
| gap-minutes | 199 | 717 |
| closes with any gap | 167 | 349 |
| gap-minutes per close (95% CI, resampling closes) | 0.28 (0.24 to 0.33) | 1.02 (0.91 to 1.14) |
| edge after fees, median / p90 / p99 (c per contract) | 0.22 / 0.95 / 1.76 | 0.51 / 1.40 / 36.0 |

- Across both directions, 916 of 278,917 range-minutes (0.33%) showed a gap.
- Excluding the anomaly below, gaps of 1c or more occurred 123 times in 30 days and
  gaps of 2c or more only 6 times. The largest was 6.3c.
- Gaps are spread through the hour: they are no more common near the close than near
  the open.
- The best basket per close has a median edge of +0.12c: in a typical hour the
  cheapest moment roughly breaks even after fees, which is what a market kept
  consistent by arbitrageurs looks like.

**The one anomaly.** All 10 gaps above 7c are in one close, `KXBTC-26SEP2809`, at 58 and
59 minutes before close (the first two minutes after listing). Nine far
out-of-the-money ranges showed a 47c YES bid at once, while their ladder legs priced
them near zero (for example lower strike YES at 2c and upper strike NO at 99c). That looks like an opening placeholder or a fat-fingered quote.
Selling into it would have been worth up to 44c per contract, but the candles carry no
size, so its depth is unknown. Including it raises the per-close figure to 1.23c (CI 0.61
to 2.44c), driven entirely by that one close.

## The long-shot seller's KXBTC trades

`candle_pass` collects every range-minute in the paper seller's band (`LongshotConfig`:
YES ask 2 to 15c, 15 to 60 minutes before close; 172,631 range-minutes over 704
closes) and compares three prices, with CIs resampling closes:

| per range-minute | cents | 95% CI |
|---|---|---|
| range YES ask | 7.94 | 7.87 to 8.00 |
| ladder-implied YES (lower mid minus upper mid) | 6.50 | 6.44 to 6.55 |
| realized hit rate | 6.01 | 5.67 to 6.37 |
| ladder minus range ask | -1.44 | -1.47 to -1.41 |
| hit rate minus range ask (YES buyer's P&L) | -1.92 | -2.25 to -1.59 |
| hit rate minus ladder | -0.49 | -0.81 to -0.15 |

The range's long shot is priced above the ladder, and the ladder is priced above the
outcome. A seller at the range ask is on the right side of both gaps, so the quoted
prices give no sign that ranges were cheap. These are quotes, not fills; the paper
seller rests a bid and is filled only when someone takes it, and `docs/LONGSHOT_GAPS.md`
showed that filled orders are where the quoted edge disappears. With a hit rate near 6%,
a contract's outcome has a standard deviation of about 24c, so one or two large losers
among 33 trades easily produce -19.6c.

`check_fills` and `fill_report` do this fill by fill against the recorded books: each
paper buy's P&L splits into price vs ladder mid and outcome vs ladder mid, and the
report counts fills above the ladder's ask. That needs the recorder data below.

## Still open: the synchronized-book pass

This session ran in a cloud container without Fly credentials, so it could not download
the recorder volume. To run the full test (top-of-book depth on every leg at the same
moment, gap durations in milliseconds, recorder outages excluded) from a machine
logged in to Fly:

```sh
# read-only copy of the recorder's Parquet and the paper accounts (git-ignored data/)
fly ssh console -a edgebook-recorder -C "tar czf - -C /data book markets settlements engine" > data.tgz
mkdir -p data && tar xzf data.tgz -C data

uv run python -m edgebook.research.range_ladder data \
  --paper data/engine/paper-longshot.json --history history --out reports/range_ladder.json
```

Add `--backfill-days 30` to rebuild `history/` first. The report gives per-close gap
counts, seconds and dollars at top-of-book size, with 95% intervals resampling closes,
for all gaps, gaps lasting 1s or more, and gaps lasting 1s or more with 10+ contracts on
every leg. It also gives the 15 largest episodes and the long-shot fill decomposition.

How the book pass works:

- Books are replayed once per ticker from snapshots and deltas into a top-of-book
  series.
- A negative level size makes a book unusable until its next snapshot.
- Any silence of 5s or more across the whole stream counts as a recorder outage. Every
  book state set before an outage is dropped until that ticker's next message, so a
  frozen book never shows up as a long-lived gap.
- Fees are rounded to the cent on the lot that top-of-book depth allows.
- Fills ignore queue position and latency, so every gap found is still an upper bound.
