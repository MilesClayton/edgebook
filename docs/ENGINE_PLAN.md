# BTC 15-Minute Kalshi Prediction & Risk Engine: implementation plan

Scope: `KXBTC15M` ("simple average of the 60 one-second CF Benchmarks BRTI
prints before close ≥ the same average before open"). Settlement source is
BRTI via Kalshi's `cfbenchmarks_value` channel; exchange prices are model
*inputs* only, never the settlement reference.

No profit is promised. Prior research on 30 days of history (see
`research/`) found no edge after fees for settlement-hold, ML, longshot,
1-minute lag round-trips, ladder arbitrage, published-repo rules, or naive
market making. This engine therefore defaults to **NO TRADE** and must earn
LIVE status through out-of-sample gates.

## Spec → code map

| Spec § | Need | Existing (`src/edgebook/…`) | Gap / work |
|---|---|---|---|
| 1 | stack, modular services, env config, pytest | uv + pydantic-settings, ruff/mypy-strict CI, 94% coverage | add `requirements.txt` export, `.env.example`, FastAPI, SQLite |
| 2 crypto | multi-exchange BBO, trades, 1s/1m changes, RV, flow, OBI, discrepancies, latency/health | Coinbase 1m REST only | **Stage 1:** `feeds/` Coinbase, Kraken, Bitstamp WebSockets → `CompositeSpot` (BBO, trade flow, OBI, cross-exchange spread, staleness, latency) |
| 2 Kalshi | contracts, rules, target, time left, book, trades, positions, fills, fees | `venues/kalshi` (signed REST/WS, books, trades, BRTI + 60s avg), recorder | add positions/fills/orders reads for reconciliation (Stage 5) |
| 3 | calibrated P(YES), E[settle], uncertainty, reversal prob | HAR + Student-t pricer w/ 60s averaging & known partial average; calibration tools | **Stage 3:** `engine/forecast.py` = pricer baseline + walk-forward logistic calibration that may blend the market price; outputs p, σ, interval, reversal prob |
| 4 | executable price, EV after fees+slippage, liquidity | `core/fees`, `live/signals` edges | **Stage 4:** `engine/ev.py` depth-walked executable price, entry *and* exit fees, slippage, ROC, liquidity |
| 5 | gated entries, limit-vs-take comparison, thresholds | trade-once (manual) | **Stage 4:** `engine/decide.py` pure entry rules with every configurable threshold; NO TRADE by default |
| 6 | HOLD / REDUCE / EXIT / HEDGE / NO TRADE | none | **Stage 4:** `engine/positions.py` hold-EV vs liquidation vs hedge comparison, never assumes a perfect hedge |
| 7 | sizing + limits + kill switch | production guard only | **Stage 4:** `engine/risk.py` all limits, uncertainty-scaled sizing, cooldowns, kill switch, block on missing inputs |
| 8 | BACKTEST / PAPER / LIVE, reconciliation, idempotency | env paper/production, IOC order, guard | **Stage 5:** `engine/execution.py` one interface, three modes; PAPER default; client IDs, partial fills, uncertain-status recovery, pre-submit risk check |
| 9 | dashboard + full logging | Fly JSON store + Vercel page (pending) | **Stage 6:** FastAPI monitor (local) + SQLite log of every prediction/decision/order/fill; Vercel page reads it |
| 10 | replay backtest w/ latency, partial fills, metrics | tick replay (queue-aware maker fills), walk-forward research | **Stage 7:** engine runs unchanged inside the replay; full metric set (DD, PF, by regime / time-left / reversals) |
| 11 | workflow | — | stages below, tests each stage |

## Stages (each: code + tests + CI green before the next)

1. **Multi-exchange crypto feeds** (`feeds/`): pure parsers per venue,
   reconnecting async clients, `CompositeSpot` (median mid, per-venue
   staleness, cross-venue spread in bps, 1 s bars, trade-flow imbalance,
   top-of-book imbalance, latency).
2. **Live feature pipeline** (`engine/features.py`): point-in-time features
   from CompositeSpot + Kalshi book + BRTI (distance to target in σ, time left,
   momentum/acceleration, RV regime, flow, OBI, market-implied p).
3. **Forecast** (`engine/forecast.py`): baseline pricer → calibrated
   probability; walk-forward fit; logged feature snapshots.
4. **EV, decisions, positions, risk** (`engine/ev.py`, `decide.py`,
   `positions.py`, `risk.py`): pure functions, exhaustively unit-tested.
5. **Execution** (`engine/execution.py`): BACKTEST/PAPER/LIVE behind one
   interface; reconciliation; LIVE requires mode + flag + viability report.
6. **Monitoring + logs**: SQLite (`engine/store.py`), FastAPI (`engine/api.py`).
7. **Replay backtest + report** on recorder data (needs ≥ several days incl.
   US hours), then **paper trading**; LIVE only after gates pass.

## Gates to LIVE (unchanged from SPEC.md, made explicit)
Out-of-sample paper results with net P&L CI low > 0 over ≥ 500 trades,
Brier ≤ market Brier on the same decisions, max drawdown within the
configured limit, zero unreconciled orders, and an explicit LIVE flag.
