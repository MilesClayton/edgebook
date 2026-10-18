"""Public Deribit BTC options data: the DVOL index and historical option trades.

Two read-only, unauthenticated sources, both stored under a history directory:

- ``deribit_dvol_1m.parquet``: Deribit's DVOL index (30-day implied volatility, in
  percent) as 1-minute candles from ``public/get_volatility_index_data``. ``ts`` is the
  candle's *close* time (start + 60 s) so ``ts <= t`` is point-in-time.
- ``deribit_option_trades_BTC.parquet``: every BTC option trade from
  ``history.deribit.com`` (the main host forgets expired instruments). Each trade carries
  the implied volatility Deribit computed at the fill, its mark price and the Deribit BTC
  index at that moment.

Deribit's terms limit market data to personal use; this module only stores it locally
(``history/`` is git-ignored) and research write-ups publish aggregates only. Re-running
skips minutes and trades already stored.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from edgebook.recorder.rows import make_schema

log = logging.getLogger(__name__)

MAIN = "https://www.deribit.com/api/v2/public"
HISTORY = "https://history.deribit.com/api/v2/public"
PAUSE_S = 0.15  # stay far below Deribit's public rate limit
PAGE = 1000
DVOL_FILE = "deribit_dvol_1m.parquet"
TRADES_FILE = "deribit_option_trades_BTC.parquet"
DELIVERY_HOUR_UTC = 8  # Deribit options expire at 08:00 UTC

DVOL = make_schema(("ts", pa.int64()), ("dvol", pa.float64()))
TRADES = make_schema(
    ("trade_id", pa.string()),
    ("ts_ms", pa.int64()),
    ("instrument", pa.string()),
    ("iv", pa.float64()),
    ("price", pa.float64()),
    ("mark_price", pa.float64()),
    ("index_price", pa.float64()),
    ("amount", pa.float64()),
    ("direction", pa.string()),
)

MONTHS = {
    m: i
    for i, m in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
    )
}


@dataclass(frozen=True)
class Option:
    expiry_ts: int  # Unix s, 08:00 UTC on the expiry date
    strike: float
    call: bool


def parse_instrument(name: str) -> Option | None:
    """``BTC-17SEP26-71000-P`` -> expiry 2026-09-17 08:00 UTC, strike 71000, put."""
    parts = name.split("-")
    if len(parts) != 4 or parts[3] not in ("C", "P"):
        return None
    date = parts[1]
    try:
        day, mon, yy = int(date[:-5]), MONTHS[date[-5:-2]], int(date[-2:])
        strike = float(parts[2].replace("d", "."))
    except (KeyError, ValueError):
        return None
    expiry = datetime(2000 + yy, mon, day, DELIVERY_HOUR_UTC, tzinfo=UTC)
    return Option(int(expiry.timestamp()), strike, parts[3] == "C")


def _get(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(5):
        time.sleep(PAUSE_S * (1 + 4 * attempt))
        try:
            resp = client.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()["result"]
                return result
            log.warning("deribit %s -> %s", url, resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("deribit %s failed: %s", url, exc)
    raise RuntimeError(f"deribit request kept failing: {url} {params}")


def dvol_pages(client: httpx.Client, start_s: int, end_s: int) -> Iterator[list[list[float]]]:
    """DVOL candles newest-first; each call returns <= 1000 and a backwards cursor."""
    end_ms = end_s * 1000
    while end_ms > start_s * 1000:
        res = _get(
            client,
            f"{MAIN}/get_volatility_index_data",
            {
                "currency": "BTC",
                "start_timestamp": start_s * 1000,
                "end_timestamp": end_ms,
                "resolution": 60,
            },
        )
        data: list[list[float]] = res.get("data") or []
        if not data:
            return
        yield data
        nxt = res.get("continuation")
        if nxt is None or int(nxt) >= end_ms:
            return
        end_ms = int(nxt)


def trade_pages(client: httpx.Client, start_s: int, end_s: int) -> Iterator[list[dict[str, Any]]]:
    """Option trades oldest-first in pages of 1000, restarting from the last timestamp."""
    cursor = start_s * 1000
    while cursor < end_s * 1000:
        res = _get(
            client,
            f"{HISTORY}/get_last_trades_by_currency_and_time",
            {
                "currency": "BTC",
                "kind": "option",
                "start_timestamp": cursor,
                "end_timestamp": end_s * 1000,
                "count": PAGE,
                "include_old": "true",
                "sorting": "asc",
            },
        )
        trades: list[dict[str, Any]] = res.get("trades") or []
        if not trades:
            return
        yield trades
        last = int(trades[-1]["timestamp"])
        if not res.get("has_more"):
            return
        # same-millisecond trades can straddle a page; dedupe by id on write
        cursor = last if last > cursor else last + 1


def trade_row(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": str(t["trade_id"]),
        "ts_ms": int(t["timestamp"]),
        "instrument": str(t["instrument_name"]),
        "iv": float(t["iv"]) if t.get("iv") is not None else None,
        "price": float(t["price"]),
        "mark_price": float(t["mark_price"]) if t.get("mark_price") is not None else None,
        "index_price": float(t["index_price"]) if t.get("index_price") is not None else None,
        "amount": float(t["amount"]),
        "direction": str(t.get("direction", "")),
    }


def _merge(path: Path, schema: pa.Schema, rows: list[dict[str, Any]], key: str) -> int:
    old: list[dict[str, Any]] = pq.read_table(path).to_pylist() if path.exists() else []
    by = {r[key]: r for r in old}
    before = len(by)
    for r in rows:
        by[r[key]] = r
    merged = sorted(by.values(), key=lambda r: r["ts_ms" if "ts_ms" in r else "ts"])
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(merged, schema=schema), path)
    return len(by) - before


def backfill(directory: Path, start_s: int, end_s: int, client: httpx.Client) -> tuple[int, int]:
    """Fetch DVOL and option trades for [start_s, end_s); returns rows added to each."""
    dvol_rows = [
        {"ts": int(c[0]) // 1000 + 60, "dvol": float(c[4])}
        for page in dvol_pages(client, start_s, end_s)
        for c in page
    ]
    added_dvol = _merge(directory / DVOL_FILE, DVOL, dvol_rows, "ts")
    trades_path = directory / TRADES_FILE
    if trades_path.exists():  # resume after the newest stored trade
        stored = pq.read_table(trades_path, columns=["ts_ms"]).column("ts_ms").to_pylist()
        start_s = max(start_s, max((int(x) for x in stored if x is not None), default=0) // 1000)
    rows: list[dict[str, Any]] = []
    for n, page in enumerate(trade_pages(client, start_s, end_s), 1):
        rows += [trade_row(t) for t in page]
        if n % 25 == 0:
            log.info(
                "trades through %s (%d rows)",
                datetime.fromtimestamp(rows[-1]["ts_ms"] / 1000, UTC),
                len(rows),
            )
    added_trades = _merge(trades_path, TRADES, rows, "trade_id")
    return added_dvol, added_trades


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=Path("history"))
    ap.add_argument("--start", required=True, help="UTC date, e.g. 2026-09-15")
    ap.add_argument("--end", required=True, help="UTC date (exclusive)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    start = int(datetime.fromisoformat(args.start).replace(tzinfo=UTC).timestamp())
    end = int(datetime.fromisoformat(args.end).replace(tzinfo=UTC).timestamp())
    with httpx.Client(headers={"User-Agent": "edgebook-research"}) as client:
        dv, tr = backfill(args.dir, start, end, client)
    print(f"added {dv} DVOL minutes and {tr} option trades to {args.dir}")


if __name__ == "__main__":  # pragma: no cover
    main()
