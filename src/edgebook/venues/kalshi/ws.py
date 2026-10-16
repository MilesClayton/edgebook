"""Kalshi WebSocket client with sequence-gap detection and auto-reconnect.

The connection requires a signed handshake even for public channels. Messages
are parsed into typed models; anything unparseable is logged and dropped.
A sequence gap on an orderbook subscription means local book state can no
longer be trusted, so the client reconnects and receives fresh snapshots.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from edgebook.venues.kalshi.auth import KalshiSigner
from edgebook.venues.kalshi.models import (
    WS_MESSAGE,
    Ack,
    DataMessage,
    OrderbookDelta,
    OrderbookSnapshot,
    Subscribed,
    WsError,
)

log = logging.getLogger(__name__)

KALSHI_WS_URLS = {
    "production": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    "paper": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
}
MAX_BACKOFF_S = 30.0
SUBSCRIBE_BATCH = 100  # tickers per subscribe command


class SequenceGapError(RuntimeError):
    pass


@dataclass
class _SeqTracker:
    last: dict[int, int] = field(default_factory=dict)

    def check(self, sid: int, seq: int) -> bool:
        """Record ``seq`` for ``sid``; False if a message was skipped."""
        prev = self.last.get(sid)
        self.last[sid] = seq
        return prev is None or seq == prev + 1


@dataclass(frozen=True)
class Subscription:
    channels: tuple[str, ...]
    market_tickers: tuple[str, ...] = ()
    index_ids: tuple[str, ...] = ()

    def params(self) -> dict[str, Any]:
        p: dict[str, Any] = {"channels": list(self.channels)}
        if self.market_tickers:
            p["market_tickers"] = list(self.market_tickers)
        if self.index_ids:
            p["index_ids"] = list(self.index_ids)
        return p


class KalshiWebSocket:
    def __init__(
        self,
        url: str,
        signer: KalshiSigner,
        connector: Callable[..., Any] = connect,
    ) -> None:
        self._url = url
        self._signer = signer
        self._connector = connector
        self._subs: list[Subscription] = []
        self._ws: ClientConnection | None = None
        self._next_id = 1
        self.reconnects = 0
        self.gaps = 0

    def add(self, sub: Subscription) -> None:
        """Register a subscription; sent now if connected and on every reconnect."""
        self._subs.append(sub)
        if self._ws is not None:
            task = asyncio.ensure_future(self._send_subscribe(self._ws, sub))
            task.add_done_callback(_log_task_error)

    def add_markets(self, channels: Sequence[str], tickers: Sequence[str]) -> None:
        for i in range(0, len(tickers), SUBSCRIBE_BATCH):
            batch = tuple(tickers[i : i + SUBSCRIBE_BATCH])
            self.add(Subscription(channels=tuple(channels), market_tickers=batch))

    async def _send_subscribe(self, ws: ClientConnection, sub: Subscription) -> None:
        cmd_id = self._next_id
        self._next_id += 1
        await ws.send(json.dumps({"id": cmd_id, "cmd": "subscribe", "params": sub.params()}))

    async def messages(self) -> AsyncIterator[DataMessage]:
        """Yield data messages forever, reconnecting with jittered backoff."""
        backoff = 1.0
        while True:
            try:
                headers = self._signer.headers("GET", self._url)
                async with self._connector(self._url, additional_headers=headers) as ws:
                    self._ws = ws
                    for sub in list(self._subs):
                        await self._send_subscribe(ws, sub)
                    backoff = 1.0
                    async for msg in self._read(ws):
                        yield msg
            except (ConnectionClosed, OSError, SequenceGapError) as exc:
                log.warning("kalshi ws reconnecting after %s: %s", type(exc).__name__, exc)
            finally:
                self._ws = None
            self.reconnects += 1
            await asyncio.sleep(backoff * (0.5 + random.random()))  # noqa: S311
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _read(self, ws: ClientConnection) -> AsyncIterator[DataMessage]:
        seqs = _SeqTracker()
        async for raw in ws:
            try:
                msg = WS_MESSAGE.validate_json(raw)
            except ValidationError as exc:
                log.debug("dropping unparsed kalshi message: %s (%s)", raw[:200], exc)
                continue
            if isinstance(msg, Subscribed):
                continue
            if isinstance(msg, WsError):
                log.error("kalshi ws error %s: %s", msg.msg.code, msg.msg.msg)
            if isinstance(msg, Ack | WsError):
                if msg.sid is not None and msg.seq is not None:
                    seqs.check(msg.sid, msg.seq)
                continue
            if not seqs.check(msg.sid, msg.seq):
                self.gaps += 1
                if isinstance(msg, OrderbookSnapshot | OrderbookDelta):
                    raise SequenceGapError(f"sid {msg.sid} skipped to seq {msg.seq}")
                log.warning("sequence gap on sid %s (non-book channel)", msg.sid)
            yield msg

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()


def _log_task_error(task: asyncio.Future[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("subscribe failed: %s", task.exception())
