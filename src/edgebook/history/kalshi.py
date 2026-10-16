"""Settled Kalshi markets plus their full 1-minute quote history.

Two files per series:

- ``kalshi_markets_<series>.parquet``: one row per settled market (strike,
  open/close, result, settlement value)
- ``kalshi_candles_<series>.parquet``: every 1-minute YES bid/ask/last/volume
  candle from market open (capped at ``LOOKBACK_MIN`` before close)

Only markets that were ever tradeable (mid in [0.03, 0.97] with a spread of at
most 10c) are kept; far out-of-the-money strikes carry no information. The
quote path is what market-aware features need: where the market was, how it
moved, and whether it lagged spot. ``horizon_rows`` derives the per-horizon
rows the calibration report uses. Re-running skips events already stored.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
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
LOOKBACK_MIN = 66
MIN_MID, MAX_MID, MAX_SPREAD = 0.03, 0.97, 0.10

MARKETS = make_schema(
    ("series", pa.string()),
    ("event_ticker", pa.string()),
    ("ticker", pa.string()),
    ("open_ts", pa.int64()),
    ("close_ts", pa.int64()),
    ("strike_type", pa.string()),
    ("floor", pa.float64()),
    ("cap", pa.float64()),
    ("result", pa.string()),
    ("expiration_value", pa.float64()),
)
CANDLES = make_schema(
    ("ticker", pa.string()),
    ("end_ts", pa.int64()),
    ("bid", pa.float64()),
    ("ask", pa.float64()),
    ("last", pa.float64()),
    ("volume", pa.float64()),
)


def markets_path(directory: Path, series: str) -> Path:
    return directory / f"kalshi_markets_{series}.parquet"


def candles_path(directory: Path, series: str) -> Path:
    return directory / f"kalshi_candles_{series}.parquet"


def _read(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist() if path.exists() else []
    return rows


def load_markets(directory: Path, series: str) -> list[dict[str, Any]]:
    return _read(markets_path(directory, series))


def load_candles(directory: Path, series: str) -> dict[str, list[dict[str, Any]]]:
    """Candles grouped by ticker, sorted by time."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in _read(candles_path(directory, series)):
        by[c["ticker"]].append(c)
    for cs in by.values():
        cs.sort(key=lambda c: c["end_ts"])
    return by


def _float(x: object) -> float | None:
    try:
        return float(str(x)) if x not in (None, "") else None
    except ValueError:
        return None


def tradeable(bid: float | None, ask: float | None) -> bool:
    if bid is None or ask is None or ask <= bid:
        return False
    return MIN_MID <= (bid + ask) / 2 <= MAX_MID and ask - bid <= MAX_SPREAD


def quote_at(candles: Sequence[dict[str, Any]], ts: int) -> tuple[float, float] | None:
    """Bid/ask at ``ts`` from the last candle ending at or before it (within 5 min)."""
    best = None
    for c in candles:
        if c["end_ts"] > ts:
            break
        best = c
    if best is None or ts - best["end_ts"] > 300:
        return None
    return best["bid"], best["ask"]


def market_row(series: str, m: RestMarket) -> dict[str, Any]:
    close_ts = int(m.close_time.timestamp())
    open_ts = int(m.open_time.timestamp()) if m.open_time else close_ts - LOOKBACK_MIN * 60
    return {
        "series": series,
        "event_ticker": m.event_ticker,
        "ticker": m.ticker,
        "open_ts": open_ts,
        "close_ts": close_ts,
        "strike_type": m.strike_type,
        "floor": _float(m.floor_strike),
        "cap": _float(m.cap_strike),
        "result": m.result,
        "expiration_value": _float(m.expiration_value),
    }


def candle_rows(ticker: str, candles: Iterable[MarketCandlestick]) -> list[dict[str, Any]]:
    return [
        {
            "ticker": ticker,
            "end_ts": c.end_period_ts,
            "bid": float(c.yes_bid.close_dollars),
            "ask": float(c.yes_ask.close_dollars),
            "last": float(c.price.close_dollars) if c.price.close_dollars is not None else None,
            "volume": float(c.volume_fp),
        }
        for c in candles
    ]


def horizon_rows(
    markets: Sequence[dict[str, Any]],
    candles: dict[str, list[dict[str, Any]]],
    horizons: Sequence[int] = HORIZONS_MIN,
) -> list[dict[str, Any]]:
    """One row per (market, horizon) with the quote a trader saw at close - h."""
    rows = []
    for m in markets:
        for h in horizons:
            t = m["close_ts"] - 60 * h
            if t < m["open_ts"]:
                continue
            q = quote_at(candles.get(m["ticker"], []), t)
            rows.append(
                {**m, "horizon_min": h, "bid": q[0] if q else None, "ask": q[1] if q else None}
            )
    return rows


async def backfill(
    rest: KalshiRest, directory: Path, series: str, days: int, now: datetime | None = None
) -> int:
    """Add settled events from the last ``days``; returns markets added."""
    now = now or datetime.now(UTC)
    markets = load_markets(directory, series)
    candles = _read(candles_path(directory, series))
    done = {m["event_ticker"] for m in markets}
    listed = await rest.markets(series, status=None, min_close=now - timedelta(days=days))
    by_event: dict[str, list[RestMarket]] = defaultdict(list)
    for m in listed:
        if m.result in ("yes", "no") and m.event_ticker not in done and m.close_time <= now:
            by_event[m.event_ticker].append(m)
    added = 0
    for i, (_event, ms) in enumerate(sorted(by_event.items())):
        close = max(m.close_time for m in ms)
        opened = min((m.open_time for m in ms if m.open_time), default=close)
        start = max(opened, close - timedelta(minutes=LOOKBACK_MIN))
        got = await rest.candlesticks([m.ticker for m in ms], start=start, end=close)
        for m in ms:
            rows = candle_rows(m.ticker, got.get(m.ticker, []))
            if any(tradeable(r["bid"], r["ask"]) for r in rows):
                markets.append(market_row(series, m))
                candles.extend(rows)
                added += 1
        if i % 100 == 0:
            log.info("%s: %d/%d events", series, i + 1, len(by_event))
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(markets, schema=MARKETS), markets_path(directory, series))
    pq.write_table(pa.Table.from_pylist(candles, schema=CANDLES), candles_path(directory, series))
    return added
