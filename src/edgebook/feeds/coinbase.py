"""Coinbase Exchange public level-2 and match feed.

Message formats follow the public ``level2_batch`` and ``matches`` channels:
https://docs.cdp.coinbase.com/exchange/websocket-feed/channels
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
WS_URL = "wss://ws-feed.exchange.coinbase.com"
MAX_BACKOFF_S = 30.0


class CoinbaseParser:
    """Stateful normalizer for Coinbase level-2 and match messages."""

    def __init__(self, product_id: str = "BTC-USD") -> None:
        self.product_id = product_id
        self._books: dict[str, tuple[dict[float, float], dict[float, float]]] = {}

    def reset(self) -> None:
        self._books.clear()

    def parse(self, message: str | bytes, received_ms: int) -> list[FeedEvent]:
        data = decode_object(message)
        if data is None or data.get("product_id") != self.product_id:
            return []
        kind = data.get("type")
        event_ms = timestamp_ms(data.get("time"), received_ms)
        if kind == "match":
            price = number(data.get("price"))
            size = number(data.get("size"))
            maker_side = data.get("side")
            if price is None or size is None or price <= 0 or size <= 0:
                return []
            if maker_side not in ("buy", "sell"):
                return []
            taker_side: Side = "sell" if maker_side == "buy" else "buy"
            return [
                TradeTick(
                    venue="coinbase",
                    symbol=self.product_id,
                    side=taker_side,
                    price=price,
                    size=size,
                    event_ts_ms=event_ms,
                    received_ts_ms=received_ms,
                )
            ]

        if kind == "snapshot":
            bids: dict[float, float] = {}
            asks: dict[float, float] = {}
            self._books[self.product_id] = (bids, asks)
            self._load_levels(bids, data.get("bids"))
            self._load_levels(asks, data.get("asks"))
        elif kind == "l2update":
            bids, asks = self._books.setdefault(self.product_id, ({}, {}))
            changes = data.get("changes")
            if not isinstance(changes, list):
                return []
            for change in changes:
                if not isinstance(change, list) or len(change) != 3:
                    continue
                side, raw_price, raw_size = change
                levels = bids if side == "buy" else asks if side == "sell" else None
                price, size = number(raw_price), number(raw_size)
                if levels is None or price is None or size is None or price <= 0 or size < 0:
                    continue
                if size == 0:
                    levels.pop(price, None)
                else:
                    levels[price] = size
        else:
            return []

        quote = self._quote(event_ms, received_ms)
        return [] if quote is None else [quote]

    @staticmethod
    def _load_levels(levels: dict[float, float], rows: object) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                continue
            price, size = number(row[0]), number(row[1])
            if price is not None and size is not None and price > 0 and size > 0:
                levels[price] = size

    def _quote(self, event_ms: int, received_ms: int) -> BookQuote | None:
        book = self._books.get(self.product_id)
        if book is None:
            return None
        bids, asks = book
        if not bids or not asks:
            return None
        bid, ask = max(bids), min(asks)
        if bid >= ask:
            return None
        return BookQuote(
            venue="coinbase",
            symbol=self.product_id,
            bid=bid,
            bid_size=bids[bid],
            ask=ask,
            ask_size=asks[ask],
            event_ts_ms=event_ms,
            received_ts_ms=received_ms,
        )


class CoinbaseFeed:
    """Reconnect indefinitely and yield normalized Coinbase spot events."""

    def __init__(
        self,
        product_id: str = "BTC-USD",
        connector: Callable[..., Any] = connect,
    ) -> None:
        self.product_id = product_id
        self.parser = CoinbaseParser(product_id)
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
                                "type": "subscribe",
                                "product_ids": [self.product_id],
                                "channels": ["level2_batch", "matches", "heartbeat"],
                            }
                        )
                    )
                    async for message in ws:
                        received_ms = time.time_ns() // 1_000_000
                        for event in self.parser.parse(message, received_ms):
                            yield event
                    log.warning("coinbase feed closed; reconnecting")
            except (WebSocketException, OSError, TimeoutError) as exc:
                log.warning("coinbase feed reconnecting after %s", type(exc).__name__)
            finally:
                self.reconnects += 1
                if connected_at is not None and time.monotonic() - connected_at >= 60:
                    backoff = 1.0
            await asyncio.sleep(backoff * (0.5 + random.random()))  # noqa: S311
            backoff = min(backoff * 2, MAX_BACKOFF_S)
