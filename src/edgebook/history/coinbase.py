"""BTC-USD 1-minute closes from Coinbase Exchange's public candles API.

Coinbase is one of the BRTI constituent exchanges, so its 1-minute closes are
a close proxy for the settlement index over the history Kalshi doesn't stream
to us. ``calibrate`` reports the tracking error against Kalshi's actual
settlement values so the proxy's quality is measured, not assumed.

Stored as ``<dir>/spot_1m_<product>.parquet`` with columns ``ts`` (Unix
seconds of the close, i.e. candle start + 60) and ``close``. Re-running only
fetches minutes after the last stored one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from edgebook.recorder.rows import make_schema
from edgebook.venues.kalshi.ratelimit import TokenBucket

BASE = "https://api.exchange.coinbase.com"
PER_REQUEST = 300  # Coinbase max candles per call
SCHEMA = make_schema(("ts", pa.int64()), ("close", pa.float64()))


def path_for(directory: Path, product: str) -> Path:
    return directory / f"spot_1m_{product}.parquet"


def load(directory: Path, product: str = "BTC-USD") -> tuple[np.ndarray, np.ndarray]:
    table = pq.read_table(path_for(directory, product))
    return (
        table.column("ts").to_numpy().astype(np.int64),
        table.column("close").to_numpy().astype(np.float64),
    )


async def fetch(
    client: httpx.AsyncClient,
    start: datetime,
    end: datetime,
    product: str = "BTC-USD",
    bucket: TokenBucket | None = None,
) -> dict[int, float]:
    """Close price keyed by close time (Unix s) for minutes in [start, end)."""
    bucket = bucket or TokenBucket(5, capacity=5)
    out: dict[int, float] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(minutes=PER_REQUEST), end)
        await bucket.acquire(1)
        resp = await client.get(
            f"{BASE}/products/{product}/candles",
            params={
                "granularity": 60,
                "start": cursor.isoformat(),
                "end": chunk_end.isoformat(),
            },
        )
        resp.raise_for_status()
        for row in resp.json():  # [time, low, high, open, close, volume], newest first
            start_s = int(row[0])
            if int(start.timestamp()) <= start_s < int(end.timestamp()):
                out[start_s + 60] = float(row[4])
        cursor = chunk_end
    return out


async def backfill(
    directory: Path,
    days: int,
    product: str = "BTC-USD",
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> int:
    """Extend the stored series to cover the last ``days`` up to ``now``; returns rows added."""
    now = (now or datetime.now(UTC)).replace(second=0, microsecond=0)
    path = path_for(directory, product)
    existing: dict[int, float] = {}
    if path.exists():
        ts, close = load(directory, product)
        existing = dict(zip(ts.tolist(), close.tolist(), strict=True))
    start = now - timedelta(days=days)
    # Fill whatever the stored series is missing on either side of the window.
    # Candle times are starts; a stored close at T came from the candle starting T - 60.
    ranges = [(start, now)]
    if existing:
        first = datetime.fromtimestamp(min(existing) - 60, tz=UTC)
        last = datetime.fromtimestamp(max(existing), tz=UTC)
        ranges = [(start, first), (max(start, last), now)]
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    new: dict[int, float] = {}
    try:
        for lo, hi in ranges:
            if lo < hi:
                new.update(await fetch(client, lo, hi, product))
    finally:
        if own:
            await client.aclose()
    added = sum(1 for k in new if k not in existing)
    existing.update(new)
    keys = sorted(existing)  # never prune: a shorter --days must not delete history
    directory.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {"ts": [int(k) for k in keys], "close": [existing[k] for k in keys]}, schema=SCHEMA
    )
    pq.write_table(table, path)
    return added
