"""A restart reloads recorded index history instead of re-running the 15-minute warm-up."""

import json
from decimal import Decimal
from pathlib import Path

from edgebook.engine.seed import recent_index, seed_index
from edgebook.engine.state import LiveState
from edgebook.recorder.sink import ParquetSink

NOW = 1_792_300_000_000


def record(root: Path, ticks: list[tuple[int, float]], index_id: str = "BRTI") -> None:
    sink = ParquetSink(root)
    for ms, value in ticks:
        sink.add(
            "index",
            {
                "recv_ts_ms": ms,
                "received_at_ms": ms,
                "index_id": index_id,
                "avg_60s": Decimal("1"),
                "window_start_ms": ms,
                "window_end_ms": ms,
                "raw": json.dumps({"value": value}),
            },
        )
    sink.flush_all()


def test_restart_reloads_recent_history(tmp_path: Path) -> None:
    ticks = [(NOW - 1_200_000 + i * 1000, 82_000.0 + i) for i in range(1_150)]  # 20 min to -50 s
    record(tmp_path, [(NOW - 7_200_000, 1.0)])  # older than the 30-min window: ignored
    record(tmp_path, ticks)
    record(tmp_path, [(NOW - 10_000, 9.0)], index_id="OTHER")
    state = LiveState()
    assert seed_index(state, tmp_path, NOW) == 1_150
    assert state.history_s(NOW) >= 900  # past the warm-up gate right away
    assert state.index_now() == ticks[-1]
    assert seed_index(state, tmp_path, NOW) == 0  # never twice / over live data


def test_long_outage_still_warms_up(tmp_path: Path) -> None:
    record(tmp_path, [(NOW - 900_000 + i * 1000, 82_000.0) for i in range(100)])  # ends 13 min ago
    state = LiveState()
    assert seed_index(state, tmp_path, NOW) == 0
    assert not state.index
    assert recent_index(tmp_path / "missing", "BRTI", NOW) == []
    (tmp_path / "index" / "date=2026-10-18" / "part-x-1.parquet").write_bytes(b"")
    (tmp_path / "index" / "date=2026-10-18" / f"part-{NOW}-9.parquet").write_bytes(b"torn")
    assert len(recent_index(tmp_path, "BRTI", NOW)) == 100
