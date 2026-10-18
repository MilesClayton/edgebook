"""Reload recent settlement-index history after a restart, so the engine doesn't sit out
the 15-minute warm-up every time the app is deployed.

The recorder writes every index tick to ``<data>/index/date=YYYY-MM-DD/part-<first_ms>-n``.
Only a short gap is bridged: if the newest recorded tick is older than ``max_gap_s``
(a real outage), nothing is loaded and the engine warms up from live data as before."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds

from edgebook.engine.state import INDEX_KEEP_S, LiveState

log = logging.getLogger(__name__)

PART_SPAN_MS = 10 * 60 * 1000  # a part file never spans more than this (sink flushes each 60 s)


def recent_index(
    data_dir: Path, index_id: str, now_ms: int, keep_s: int = INDEX_KEEP_S
) -> list[tuple[int, float]]:
    """Recorded (recv_ms, value) ticks for ``index_id`` from the last ``keep_s`` seconds."""
    start_ms = now_ms - keep_s * 1000
    root = data_dir / "index"
    days = {
        datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d") for ms in (start_ms, now_ms)
    }
    files = []
    for day in sorted(days):
        for f in (root / f"date={day}").glob("part-*.parquet"):
            try:
                first_ms = int(f.name.split("-")[1])
            except (IndexError, ValueError):
                continue
            if first_ms >= start_ms - PART_SPAN_MS and f.stat().st_size > 0:
                files.append(f)
    flt = (ds.field("recv_ts_ms") >= start_ms) & (ds.field("recv_ts_ms") <= now_ms)
    ticks: list[tuple[int, float]] = []
    for f in files:
        try:
            rows = ds.dataset(f).to_table(columns=["recv_ts_ms", "index_id", "raw"], filter=flt)
        except (pa.ArrowInvalid, OSError):
            continue  # torn part file from a restart mid-write
        for r in rows.to_pylist():
            if r["index_id"] != index_id:
                continue
            try:
                ticks.append((int(r["recv_ts_ms"]), float(json.loads(r["raw"])["value"])))
            except (ValueError, KeyError, TypeError):
                continue
    return sorted(ticks)


def seed_index(state: LiveState, data_dir: Path, now_ms: int, max_gap_s: float = 300) -> int:
    """Pre-load recent index ticks into ``state``; returns how many were loaded."""
    if state.index:
        return 0
    ticks = recent_index(data_dir, state.index_id, now_ms)
    if not ticks or now_ms - ticks[-1][0] > max_gap_s * 1000:
        return 0
    state.index.extend(ticks)
    log.info("seeded %d index ticks (%.0f s of history)", len(ticks), state.history_s(now_ms))
    return len(ticks)
