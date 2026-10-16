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

## Layout
```
src/edgebook/
  core/     domain types (Price, OrderBook, Order, Fill...) and fee math
  config.py settings, environments, production guard
```
Upcoming (see SPEC.md milestones): `venues/kalshi`, `feeds`, `recorder`, `model`, `strategy`, `risk`, `execution`, `backtest`, `app`.
