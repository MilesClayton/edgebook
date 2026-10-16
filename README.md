# edgebook

Typed Python quant trading bot for prediction markets. Kalshi BTC binaries (`KXBTCD`, `KXBTC15M`) are venue/asset one; the core is venue- and asset-agnostic.

No LLM in the decision path: a deterministic model prices each strike, trades only when edge beats fees, and production order entry is locked until paper trading proves a positive post-fee edge. See `SPEC.md` for the full plan.

## Environments
| `EDGEBOOK_ENV` | Orders go to | Market data from |
|---|---|---|
| `paper` (default) | Kalshi demo | Kalshi production (read-only) |
| `production` | Kalshi production | Kalshi production |

Production refuses to trade unless `EDGEBOOK_LIVE_TRADING_ENABLED=true` **and** `viability.json` shows at least `EDGEBOOK_MIN_VIABILITY_TRADES` paper trades with a 95% CI low above zero (`edgebook.config.assert_live_trading_allowed`).

## Develop
```bash
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy          # strict
uv run pytest        # includes hypothesis property tests, 85% coverage floor
```

## Workflow
Trunk-based: small single-purpose PRs into `main`, squash-merge only, linear history. `main` is protected and requires the `ci-ok` check (lint, types, tests, dependency audit).

## Commands
```bash
uv run python -m edgebook markets   # open markets per series (public, no key)
uv run python -m edgebook balance   # verify a key against EDGEBOOK_ENV (signed)
uv run python -m edgebook record    # stream prod books/trades/BRTI to ./data (needs a prod key)
```
Keys: `EDGEBOOK_KALSHI_KEY_ID` plus `EDGEBOOK_KALSHI_PRIVATE_KEY` (PEM content) or `EDGEBOOK_KALSHI_PRIVATE_KEY_PATH`. Never commit `.pem` files.

## Recorded data (`data/<stream>/date=YYYY-MM-DD/*.parquet`)
| Stream | Contents |
|---|---|
| `book` | orderbook `reset` + `level` rows per snapshot, then signed `delta` rows, with `sid`/`seq` |
| `trades` | every public trade: price, size, taker side |
| `index` | CF Benchmarks BRTI ticks incl. Kalshi's rolling 60-second settlement average |
| `markets` | each market when first seen: strike, strike type, close time |
| `settlements` | final result + expiration value, for model calibration |

The recorder resubscribes on sequence gaps (fresh snapshots) and reconnects with jittered backoff.

## Deploy (Fly.io)
The recorder runs as `edgebook-recorder` in `iad`; merges to `main` deploy it via `.github/workflows/deploy.yml`.

One-time setup:
```bash
fly auth login
fly apps create edgebook-recorder
fly volumes create recorder_data --app edgebook-recorder --region iad --size 40   # ~1.5GB/day; extend with `fly volumes extend`
fly secrets set --app edgebook-recorder EDGEBOOK_KALSHI_KEY_ID=<prod key id> \
  EDGEBOOK_KALSHI_PRIVATE_KEY="$(cat ~/path/to/kalshi-prod.pem)"
fly tokens create deploy --app edgebook-recorder   # store as FLY_API_TOKEN in the GitHub "paper" environment
```

## Layout
```
src/edgebook/
  core/            domain types, fee math, BookState (snapshot + delta)
  config.py        settings, environments, production guard
  venues/kalshi/   signing, rate limiter, typed wire models, REST + WebSocket clients
  recorder/        message -> Parquet rows, buffered sink, discovery/settlement loops
  app/cli.py       entrypoints
tests/fixtures/kalshi/  committed OpenAPI/AsyncAPI snapshot (refresh: scripts/update_kalshi_specs.sh)
```
Upcoming (see SPEC.md milestones): `model`, `strategy`, `risk`, `execution`, `backtest`.
