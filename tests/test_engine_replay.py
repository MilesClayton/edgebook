"""Replaying recorded Parquet through the live engine and its paper accounts."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from edgebook.engine.loop import DEFAULT_STRATEGIES
from edgebook.research import engine_replay

START_MS = 1_792_130_400_000
CLOSE_S = START_MS // 1000 + 1200


def write(directory: Path, stream: str, rows: list[dict[str, Any]]) -> None:
    part = directory / stream / "date=2026-10-16"
    part.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), part / f"part-{START_MS}-1.parquet")


def recording(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    market = {
        "series": "KXBTC15M",
        "ticker": "M",
        "close_time": datetime.fromtimestamp(CLOSE_S, tz=UTC),
        "strike_type": "greater_or_equal",
        "floor_strike": 82_000.0,
        "cap_strike": None,
    }
    write(d, "markets", [{**market, "result": ""}])
    write(d, "settlements", [{**market, "result": "yes"}])
    book = [
        {
            "recv_ts_ms": START_MS - 5,
            "kind": "delta",
            "ticker": "M",
            "side": "yes",
            "price": "0.40",
            "qty": "5",
        },
        {
            "recv_ts_ms": START_MS,
            "kind": "reset",
            "ticker": "M",
            "side": None,
            "price": None,
            "qty": None,
        },
        {
            "recv_ts_ms": START_MS,
            "kind": "level",
            "ticker": "M",
            "side": "yes",
            "price": "0.4800",
            "qty": "500",
        },
        {
            "recv_ts_ms": START_MS,
            "kind": "level",
            "ticker": "M",
            "side": "no",
            "price": "0.5100",
            "qty": "500",
        },
        {
            "recv_ts_ms": START_MS + 10,
            "kind": "delta",
            "ticker": "M",
            "side": "no",
            "price": "0.3000",
            "qty": "-9",
        },
        {
            "recv_ts_ms": START_MS + 20,
            "kind": "reset",
            "ticker": "M",
            "side": None,
            "price": None,
            "qty": None,
        },
        {
            "recv_ts_ms": START_MS + 20,
            "kind": "level",
            "ticker": "M",
            "side": "yes",
            "price": "0.4800",
            "qty": "500",
        },
        {
            "recv_ts_ms": START_MS + 20,
            "kind": "level",
            "ticker": "M",
            "side": "no",
            "price": "0.5100",
            "qty": "500",
        },
    ]
    write(d, "book", book)
    index = []
    for i in range(1000):
        v = 82_200.0 * (1 + 3e-5 * (1 if i % 2 else -1) / 2)
        raw = json.dumps({"value": f"{v:.2f}"})
        index.append({"recv_ts_ms": START_MS + i * 1000, "index_id": "BRTI", "raw": raw})
    index.append({"recv_ts_ms": START_MS + 1000, "index_id": "BRTI", "raw": "not json"})
    write(d, "index", index)
    trade = {"ticker": "M", "yes_price": 0.47, "count": 2000.0, "taker_side": "no"}
    write(d, "trades", [{"recv_ts_ms": START_MS + 960_000, **trade}])
    return d


def test_replay_runs_live_strategies_on_recorded_data(tmp_path: Path) -> None:
    d = recording(tmp_path)
    markets = engine_replay.load_markets(d)
    assert markets["M"].result == "yes"
    assert markets["M"].info.open_ts == CLOSE_S - 900
    end_ms = (CLOSE_S + 30) * 1000
    res = engine_replay.replay(d, tmp_path / "acct", DEFAULT_STRATEGIES, START_MS, end_ms)
    assert res.ticks == (end_ms - START_MS) // 1000
    by = {a["name"]: a for a in res.accounts}
    assert set(by) == {"v1", "anchored", "maker"}
    for name in ("v1", "anchored", "maker"):
        assert by[name]["closed_trades"] == 1, name  # bought UP, settled YES
        assert by[name]["equity"] > 500_000, name
        assert by[name]["holdings"] == []
    maker_buy = next(f for f in by["maker"]["fills"] if f["action"] == "BUY")
    assert maker_buy["price"] == 0.48  # filled at its resting bid, not the ask
    assert engine_replay.load_markets(tmp_path / "nothing") == {}
