"""Buffered, crash-safe Parquet writer.

Rows are buffered per stream and flushed to
``<root>/<stream>/date=YYYY-MM-DD/part-<first_recv_ms>-<n>.parquet``. Each file
is written to a temp name and renamed, so a crash never leaves a torn file;
at most one flush interval of data is lost.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from edgebook.recorder.rows import SCHEMAS, Row


class ParquetSink:
    def __init__(
        self,
        root: Path,
        max_rows: int = 50_000,
        max_age_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root
        self.max_rows = max_rows
        self.max_age_s = max_age_s
        self._clock = clock
        self._buf: dict[str, list[Row]] = {s: [] for s in SCHEMAS}
        self._started: dict[str, float] = {}
        self._seq = 0
        self.rows_written: dict[str, int] = dict.fromkeys(SCHEMAS, 0)

    def add(self, stream: str, row: Row) -> None:
        buf = self._buf[stream]
        if not buf:
            self._started[stream] = self._clock()
        buf.append(row)
        if len(buf) >= self.max_rows:
            self.flush(stream)

    def flush_due(self) -> None:
        now = self._clock()
        for stream, buf in self._buf.items():
            if buf and now - self._started[stream] >= self.max_age_s:
                self.flush(stream)

    def flush_all(self) -> None:
        for stream in self._buf:
            self.flush(stream)

    def flush(self, stream: str) -> Path | None:
        rows = self._buf[stream]
        if not rows:
            return None
        self._buf[stream] = []
        first_ms = int(rows[0].get("recv_ts_ms") or rows[0].get("seen_ts_ms") or 0)
        day = datetime.fromtimestamp(first_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        out_dir = self.root / stream / f"date={day}"
        out_dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        final = out_dir / f"part-{first_ms}-{self._seq}.parquet"
        tmp = final.with_suffix(".parquet.tmp")
        table = pa.Table.from_pylist(rows, schema=SCHEMAS[stream])
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, final)
        self.rows_written[stream] += len(rows)
        return final
