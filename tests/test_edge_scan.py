"""The three-edge scan on a small synthetic recording."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from edgebook.feeds.types import CompositeSnapshot, VenueHealth
from edgebook.recorder.rows import SCHEMAS, spot_row
from edgebook.research import edge_scan as es

START_MS = 1_792_130_400_000
CLOSE_S = START_MS // 1000 + 1200


def write(d: Path, stream: str, rows: list[dict[str, Any]]) -> None:
    part = d / stream / "date=2026-10-16"
    part.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), part / f"part-{START_MS}-1.parquet")


def market(ticker: str, series: str, floor: float, stype: str, result: str) -> dict[str, Any]:
    return {
        "series": series,
        "ticker": ticker,
        "close_time": datetime.fromtimestamp(CLOSE_S, tz=UTC),
        "strike_type": stype,
        "floor_strike": floor,
        "cap_strike": None,
        "result": result,
    }


def book_rows(ticker: str, yes_bid: str, no_bid: str) -> list[dict[str, Any]]:
    base = {"recv_ts_ms": START_MS, "ticker": ticker}
    return [
        {**base, "kind": "reset", "side": None, "price": None, "qty": None},
        {**base, "kind": "level", "side": "yes", "price": yes_bid, "qty": "500"},
        {**base, "kind": "level", "side": "no", "price": no_bid, "qty": "400"},
    ]


def recording(tmp_path: Path) -> Path:
    d = tmp_path / "rec"
    mk = [
        market("F", "KXBTC15M", 82_000.0, "greater_or_equal", "yes"),
        market("HA", "KXBTCD", 82_050.0, "greater", "yes"),
        market("HB", "KXBTCD", 81_950.0, "greater", "yes"),
    ]
    write(d, "markets", mk)
    write(d, "settlements", mk)
    book = (
        book_rows("F", "0.60", "0.38")
        + book_rows("HA", "0.70", "0.28")
        + book_rows("HB", "0.80", "0.18")
    )
    # a later update to HA so parity is re-checked after the throttle window
    book.append(
        {
            "recv_ts_ms": START_MS + 1000,
            "kind": "delta",
            "ticker": "HA",
            "side": "yes",
            "price": "0.70",
            "qty": "10",
        }
    )
    book.append(
        {
            "recv_ts_ms": CLOSE_S * 1000 - 5000,
            "kind": "delta",
            "ticker": "HA",
            "side": "yes",
            "price": "0.70",
            "qty": "-10",
        }
    )
    write(d, "book", book)
    index = []
    for i in range(1200):
        v = 82_200.0 * (1 + 3e-5 * (1 if i % 2 else -1) / 2) * (1.0005 if i >= 1000 else 1.0)
        index.append(
            {
                "recv_ts_ms": START_MS + i * 1000,
                "index_id": "BRTI",
                "raw": json.dumps({"value": f"{v:.2f}"}),
            }
        )
    write(d, "index", index)
    write(
        d,
        "trades",
        [
            {
                "recv_ts_ms": START_MS + 5,
                "ticker": "F",
                "yes_price": 0.6,
                "count": 1.0,
                "taker_side": "yes",
            }
        ],
    )
    return d


def test_scan_finds_parity_jumps_and_final_minute(tmp_path: Path) -> None:
    res = es.scan(recording(tmp_path), START_MS, (CLOSE_S + 5) * 1000)
    par = es.parity_report(res)
    assert par["closes_checked"] == 1
    assert par["closes_with_arb"] == 1  # 15m UP 0.62 + HA NO 0.30 + fees < $1
    [arb] = par["arbs"]
    assert arb["min_cost"] < 1
    assert "HA NO" in arb["pair"]
    assert arb["seconds_below_1"] > 0
    assert arb["size"] == 400
    mk = es.markout_report(res)
    assert mk["KXBTC15M"]["jumps"] >= 1
    assert mk["KXBTC15M"]["1s"]["n"] >= 1
    assert mk["KXBTC15M"]["hold"]["mean"] == pytest.approx(1 - 0.62 - es.taker_fee(0.62))
    win = es.window_report(res, thresholds=(0.02,))
    assert win["edge>=0.02"]["n"] >= 1
    assert win["edge>=0.02"]["mean"] > 0
    assert res.results["F"] == "yes"


def test_taker_fee_and_spot_row() -> None:
    assert es.taker_fee(0.5) == pytest.approx(0.0175)
    assert es.taker_fee(0.99, 1) == pytest.approx(0.0007)  # 0.000693 up to $0.0001
    snap = CompositeSnapshot(
        5,
        82_000.0,
        81_999.0,
        1.0,
        82_001.0,
        2.0,
        {"coinbase": VenueHealth(True, 10, 5), "kraken": VenueHealth(False, None, None)},
        1.5,
        0.2,
        -0.1,
        7.0,
    )
    row = spot_row(snap, 9)
    assert row["venues_active"] == 1
    assert row["mid"] == 82_000.0
    pa.Table.from_pylist([row], schema=SCHEMAS["spot"])  # matches the stream schema
