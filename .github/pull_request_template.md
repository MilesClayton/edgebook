## What
<!-- one small, single-purpose change -->

## Why

## Checks
- [ ] `uv run ruff check && uv run mypy && uv run pytest` pass locally
- [ ] Touches `model/` or `strategy/`? Backtest baseline reviewed
- [ ] Touches `risk/`, `execution/` or `config.py`? Production guard still enforced
