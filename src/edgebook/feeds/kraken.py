"""Kraken Spot WebSocket v2 public book and trade feed.

Message formats follow the public ``book`` and ``trade`` channels:
https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/book
https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/trade
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from edgebook.feeds._parse import decode_object, number, timestamp_ms
from edgebook.feeds.types import BookQuote, FeedEvent, Side, TradeTick

log = logging.getLogger(__name__)
WS_URL = "wss://ws.kraken.com/v2"
MAX_BACKOFF_S = 30.0


class KrakenParser:
    """Stateful normalizer for Kraken's snapshot/update book messages."""

    def __init__(self, symbol: str = "BTC/USD") -> None:
        self.symbol = symbol
        self._books: dict[str, tuple[dict[float, float], dict[float, float]]] = {}

    def reset(self) -> None:
        self._books.clear()

    def parse(self, message: str | bytes, received_ms: int) -> list[FeedEvent]:
        data = decode_object(message)
        if data is None:
            return []
        channel = data.get("channel")
        kind = data.get("type")
        rows = data.get("data")
        if not isinstance(rows, list):
            return []

        out: list[FeedEvent] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("symbol") != self.symbol:
                continue
            event_ms = timestamp_ms(row.get("timestamp"), received_ms)
            if channel == "trade":
                price = number(row.get("price"))
                size = number(row.get("qty"))
                side = row.get("side")
                if price is None or size is None or price <= 0 or size <= 0:
                    continue
                if side not in ("buy", "sell"):
                    continue
                trade_side: Side = side
                out.append(
                    TradeTick(
                        venue="kraken",
                        symbol=self.symbol,
                        side=trade_side,
                        price=price,
                        size=size,
                        event_ts_ms=event_ms,
                        received_ts_ms=received_ms,
                    )
                )
            elif channel == "book" and kind in ("snapshot", "update"):
                if kind == "snapshot":
                    self._books[self.symbol] = ({}, {})
                bids, asks = self._books.setdefault(self.symbol, ({}, {}))
                self._apply_levels(bids, row.get("bids"))
                self._apply_levels(asks, row.get("asks"))
                quote = self._quote(event_ms, received_ms)
                if quote is not None:
                    out.append(quote)
        return out

    @staticmethod
    def _apply_levels(levels: dict[float, float], rows: object) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            price, size = number(row.get("price")), number(row.get("qty"))
            if price is None or size is None or price <= 0 or size < 0:
                continue
            if size == 0:
                levels.pop(price, None)
            else:
                levels[price] = size

    def _quote(self, event_ms: int, received_ms: int) -> BookQuote | None:
        book = self._books.get(self.symbol)
        if book is None:
            return None
        bids, asks = book
        if not bids or not asks:
            return None
        bid, ask = max(bids), min(asks)
        if bid >= ask:
            return None
        return BookQuote(
            venue="kraken",
            symbol=self.symbol,
            bid=bid,
            bid_size=bids[bid],
            ask=ask,
            ask_size=asks[ask],
            event_ts_ms=event_ms,
            received_ts_ms=received_ms,
        )


class KrakenFeed:
    """Reconnect indefinitely and yield normalized Kraken spot events."""

    def __init__(
        self,
        symbol: str = "BTC/USD",
        connector: Callable[..., Any] = connect,
    ) -> None:
        self.symbol = symbol
        self.parser = KrakenParser(symbol)
        self._connector = connector
        self.reconnects = 0

    async def events(self) -> AsyncIterator[FeedEvent]:
        backoff = 1.0
        while True:
            self.parser.reset()
            connected_at: float | None = None
            try:
                async with self._connector(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                    connected_at = time.monotonic()
                    await ws.send(
                        json.dumps(
                            {
                                "method": "subscribe",
                                "params": {
                                    "channel": "book",
                                    "symbol": [self.symbol],
                                    "depth": 10,
                                    "snapshot": True,
                                },
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "method": "subscribe",
                                "params": {
                                    "channel": "trade",
                                    "symbol": [self.symbol],
                                    "snapshot": False,
                                },
                            }
                        )
                    )
                    async for message in ws:
                        received_ms = time.time_ns() // 1_000_000
                        for event in self.parser.parse(message, received_ms):
                            yield event
                    log.warning("kraken feed closed; reconnecting")
            except (WebSocketException, OSError, TimeoutError) as exc:
                log.warning("kraken feed reconnecting after %s", type(exc).__name__)
            finally:
                self.reconnects += 1
                if connected_at is not None and time.monotonic() - connected_at >= 60:
                    backoff = 1.0
            await asyncio.sleep(backoff * (0.5 + random.random()))  # noqa: S311
            backoff = min(backoff * 2, MAX_BACKOFF_S)
