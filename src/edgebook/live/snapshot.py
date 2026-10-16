"""Build the dashboard's live snapshot: spot, priced markets, recorder health."""

from __future__ import annotations

import shutil
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from edgebook.history import coinbase
from edgebook.live.signals import LivePricer, Signal, signals
from edgebook.recorder.run import Recorder
from edgebook.venues.kalshi.rest import KalshiRest

SPOT_POINTS = 240  # last 4 hours of 1-minute closes


class SnapshotBuilder:
    def __init__(
        self,
        rest: KalshiRest,
        pricer: LivePricer,
        recorder: Recorder | None,
        series: Sequence[str] = ("KXBTC15M", "KXBTCD"),
        data_dir: Path | None = None,
    ) -> None:
        self.rest = rest
        self.pricer = pricer
        self.recorder = recorder
        self.series = tuple(series)
        self.data_dir = data_dir
        self.started = time.monotonic()
        self._last: tuple[float, int] | None = None

    def recorder_stats(self) -> dict[str, Any]:
        if self.recorder is None:
            return {}
        now = time.monotonic()
        msgs = self.recorder.messages
        rate = None
        if self._last is not None and now > self._last[0]:
            rate = (msgs - self._last[1]) / (now - self._last[0])
        self._last = (now, msgs)
        stats: dict[str, Any] = {
            "messages": msgs,
            "msgs_per_s": rate,
            "markets": len(self.recorder.subscribed),
            "reconnects": self.recorder.ws.reconnects,
            "gaps": self.recorder.ws.gaps,
            "rows_written": dict(self.recorder.sink.rows_written),
            "uptime_s": now - self.started,
        }
        if self.data_dir is not None and self.data_dir.exists():
            du = shutil.disk_usage(self.data_dir)
            stats["disk_used_gb"] = du.used / 1e9
            stats["disk_total_gb"] = du.total / 1e9
        return stats

    async def build(self) -> dict[str, Any]:
        by_series: dict[str, list[Signal]] = {}
        for s in self.series:
            by_series[s] = await signals(self.rest, self.pricer, s, max_markets=12)
        ts, close = coinbase.load(self.pricer.history_dir)
        tail = list(zip(ts[-SPOT_POINTS:].tolist(), close[-SPOT_POINTS:].tolist(), strict=True))
        return {
            "updated": datetime.now(UTC),
            "spot": float(close[-1]),
            "spot_series": [[t * 1000, c] for t, c in tail],
            "signals": by_series,
            "recorder": self.recorder_stats(),
            "public_flow": self.recorder.public_flow() if self.recorder is not None else {},
        }
