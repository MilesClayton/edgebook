"""Settled Kalshi markets with their quotes at fixed horizons before close.

For every settled market in a series, record the YES bid/ask at
``close - h minutes`` for each horizon ``h``. That is exactly the information
a trader had at decision time, paired with the outcome, which is what
calibration and a first taker backtest need.

Stored as ``<dir>/kalshi_<series>.parquet``, one row per (ticker, horizon).
Re-running skips events already stored.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from edgebook.recorder.rows import make_schema
from edgebook.venues.kalshi.models import MarketCandlestick, RestMarket
from edgebook.venues.kalshi.rest import KalshiRest

log = logging.getLogger(__name__)

HORIZONS_MIN = (60, 30, 15, 5)
SCHEMA = make_schema(
    ("series", pa.string()),
    ("event_ticker", pa.string()),
    ("ticker", pa.string()),
    ("close_ts", pa.int64()),
    ("strike_type", pa.string()),
    ("floor", pa.float64()),
    ("cap", pa.float64()),
    ("result", pa.string()),
    ("expiration_value", pa.float64()),
    ("horizon_min", pa.int64()),
    ("bid", pa.float64()),
    ("ask", pa.float64()),
)


def path_for(directory: Path, series: str) -> Path:
    return directory / f"kalshi_{series}.parquet"


def load(directory: Path, series: str) -> list[dict[str, Any]]:
    path = path_for(directory, series)
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist() if path.exists() else []
    return rows


def _float(x: object) -> float | None:
    try:
        return float(str(x)) if x not in (None, "") else None
    except ValueError:
        return None


def quote_at(candles: Sequence[MarketCandlestick], ts: int) -> tuple[float, float] | None:
    """Bid/ask at ``ts`` from the last candle ending at or before it (within 5 min)."""
    best: MarketCandlestick | None = None
    for c in candles:
        if c.end_period_ts <= ts and (best is None or c.end_period_ts > best.end_period_ts):
            best = c
    if best is None or ts - best.end_period_ts > 300:
        return None
    return float(best.yes_bid.close_dollars), float(best.yes_ask.close_dollars)


def event_rows(
    series: str, markets: Sequence[RestMarket], candles: dict[str, list[MarketCandlestick]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for m in markets:
        close_ts = int(m.close_time.timestamp())
        for h in HORIZONS_MIN:
            q = quote_at(candles.get(m.ticker, []), close_ts - 60 * h)
            rows.append(
                {
                    "series": series,
                    "event_ticker": m.event_ticker,
                    "ticker": m.ticker,
                    "close_ts": close_ts,
                    "strike_type": m.strike_type,
                    "floor": _float(m.floor_strike),
                    "cap": _float(m.cap_strike),
                    "result": m.result,
                    "expiration_value": _float(m.expiration_value),
                    "horizon_min": h,
                    "bid": q[0] if q else None,
                    "ask": q[1] if q else None,
                }
            )
    return rows


async def backfill(
    rest: KalshiRest, directory: Path, series: str, days: int, now: datetime | None = None
) -> int:
    """Add settled events from the last ``days``; returns rows added."""
    now = now or datetime.now(UTC)
    rows = load(directory, series)
    done = {r["event_ticker"] for r in rows}
    markets = await rest.markets(series, status=None, min_close=now - timedelta(days=days))
    by_event: dict[str, list[RestMarket]] = defaultdict(list)
    for m in markets:
        if m.result in ("yes", "no") and m.event_ticker not in done and m.close_time <= now:
            by_event[m.event_ticker].append(m)
    added = 0
    for i, (_event, ms) in enumerate(sorted(by_event.items())):
        close = max(m.close_time for m in ms)
        candles = await rest.candlesticks(
            [m.ticker for m in ms],
            start=close - timedelta(minutes=max(HORIZONS_MIN) + 6),
            end=close,
        )
        new = event_rows(series, ms, candles)
        rows.extend(new)
        added += len(new)
        if i % 50 == 0:
            log.info("%s: %d/%d events", series, i + 1, len(by_event))
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), path_for(directory, series))
    return added
