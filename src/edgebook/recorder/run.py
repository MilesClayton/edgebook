"""Recorder orchestration.

Three concurrent loops share one sink:
- stream: WebSocket book deltas, trades and settlement-index values
- discover: polls open markets per series and subscribes new tickers
- settle: polls recently settled markets so the model can be calibrated
Market data always comes from production; this process never places orders.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from edgebook.recorder.rows import market_row, message_rows
from edgebook.recorder.sink import ParquetSink
from edgebook.venues.kalshi.rest import KalshiRest
from edgebook.venues.kalshi.ws import KalshiWebSocket, Subscription

log = logging.getLogger(__name__)

BOOK_CHANNELS = ("orderbook_delta", "trade")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Recorder:
    def __init__(
        self,
        rest: KalshiRest,
        ws: KalshiWebSocket,
        sink: ParquetSink,
        series: Sequence[str],
        index_ids: Sequence[str],
        discover_every_s: float = 60.0,
        settle_every_s: float = 300.0,
        on_minute: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self.rest = rest
        self.ws = ws
        self.sink = sink
        self.series = tuple(series)
        self.index_ids = tuple(index_ids)
        self.discover_every_s = discover_every_s
        self.settle_every_s = settle_every_s
        self.on_minute = on_minute  # e.g. publish a dashboard snapshot; failures are logged
        self.subscribed: set[str] = set()
        self.settled: set[str] = set()
        self.messages = 0

    async def discover_once(self) -> list[str]:
        new: list[str] = []
        seen = now_ms()
        for series in self.series:
            for m in await self.rest.markets(series, status="open"):
                if m.ticker not in self.subscribed:
                    self.subscribed.add(m.ticker)
                    self.sink.add("markets", market_row(series, m, seen))
                    new.append(m.ticker)
        if new:
            self.ws.add_markets(BOOK_CHANNELS, new)
            log.info("subscribed %d new markets (%d total)", len(new), len(self.subscribed))
        return new

    async def settle_once(self, lookback: timedelta = timedelta(hours=6)) -> int:
        since = datetime.now(UTC) - lookback
        count = 0
        for series in self.series:
            # Kalshi only allows min_close_ts with no status filter (or "closed").
            for m in await self.rest.markets(series, status=None, min_close=since):
                if m.ticker not in self.settled and m.result:
                    self.settled.add(m.ticker)
                    self.sink.add("settlements", market_row(series, m, now_ms()))
                    count += 1
        return count

    async def _every(self, seconds: float, name: str, fn: Callable[[], Awaitable[object]]) -> None:
        while True:
            try:
                await fn()
            except Exception:
                log.exception("%s loop failed; retrying next interval", name)
            await asyncio.sleep(seconds)

    async def stream(self) -> None:
        if self.index_ids:
            self.ws.add(Subscription(channels=("cfbenchmarks_value",), index_ids=self.index_ids))
        async for msg in self.ws.messages():
            recv = now_ms()
            self.messages += 1
            for stream, row in message_rows(msg, recv):
                self.sink.add(stream, row)

    async def _housekeeping(self) -> None:
        self.sink.flush_due()
        log.info(
            "recorder: %d msgs, %d markets, reconnects=%d gaps=%d written=%s",
            self.messages,
            len(self.subscribed),
            self.ws.reconnects,
            self.ws.gaps,
            self.sink.rows_written,
        )

    async def run(self) -> None:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._every(self.discover_every_s, "discover", self.discover_once))
                tg.create_task(self._every(self.settle_every_s, "settle", self.settle_once))
                tg.create_task(self._every(15.0, "housekeeping", self._housekeeping))
                tg.create_task(self.stream())
                if self.on_minute is not None:
                    tg.create_task(self._every(60.0, "on_minute", self.on_minute))
        finally:
            self.sink.flush_all()
            await self.ws.close()
            await self.rest.aclose()
