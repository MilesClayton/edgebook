"""Bitstamp WebSocket v2 public order-book and live-trade feed.

Channel names follow Bitstamp's public WebSocket API:
https://www.bitstamp.net/websocket/v2/
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
WS_URL = "wss://ws.bitstamp.net"
MAX_BACKOFF_S = 30.0


class BitstampParser:
    """Normalizer for Bitstamp order-book and trade notifications."""

    def __init__(self, market: str = "btcusd") -> None:
        self.market = market.lower()
        self.symbol = self.market.upper()

    def parse(self, message: str | bytes, received_ms: int) -> list[FeedEvent]:
        data = decode_object(message)
        if data is None:
            return []
        channel = data.get("channel")
        payload = data.get("data")
        if not isinstance(channel, str) or not isinstance(payload, dict):
            return []
        if channel == f"live_trades_{self.market}":
            price = number(payload.get("price_str", payload.get("price")))
            size = number(payload.get("amount_str", payload.get("amount")))
            side_raw = payload.get("type")
            side_value = number(side_raw)
            if price is None or size is None or price <= 0 or size <= 0:
                return []
            if side_value not in (0, 1):
                return []
            event_ms = self._event_ms(payload, received_ms)
            side: Side = "buy" if side_value == 0 else "sell"
            return [
                TradeTick(
                    venue="bitstamp",
                    symbol=self.symbol,
                    side=side,
                    price=price,
                    size=size,
                    event_ts_ms=event_ms,
                    received_ts_ms=received_ms,
                )
            ]

        if channel != f"order_book_{self.market}" or data.get("event") not in (
            "data",
            "snapshot",
        ):
            return []
        bids = self._levels(payload.get("bids"))
        asks = self._levels(payload.get("asks"))
        if not bids or not asks:
            return []
        bid, ask = max(bids), min(asks)
        if bid >= ask:
            return []
        return [
            BookQuote(
                venue="bitstamp",
                symbol=self.symbol,
                bid=bid,
                bid_size=bids[bid],
                ask=ask,
                ask_size=asks[ask],
                event_ts_ms=self._event_ms(payload, received_ms),
                received_ts_ms=received_ms,
            )
        ]

    @staticmethod
    def _levels(rows: object) -> dict[float, float]:
        levels: dict[float, float] = {}
        if not isinstance(rows, list):
            return levels
        for row in rows:
            if not isinstance(row, list) or len(row) < 2:
                continue
            price, size = number(row[0]), number(row[1])
            if price is not None and size is not None and price > 0 and size > 0:
                levels[price] = size
        return levels

    @staticmethod
    def _event_ms(payload: dict[str, Any], received_ms: int) -> int:
        microstamp = number(payload.get("microtimestamp"))
        if microstamp is not None and microstamp > 0:
            return int(microstamp / 1000)
        return timestamp_ms(payload.get("timestamp"), received_ms)


class BitstampFeed:
    """Reconnect indefinitely and yield normalized Bitstamp spot events."""

    def __init__(
        self,
        market: str = "btcusd",
        connector: Callable[..., Any] = connect,
    ) -> None:
        self.market = market.lower()
        self.parser = BitstampParser(market)
        self._connector = connector
        self.reconnects = 0

    async def events(self) -> AsyncIterator[FeedEvent]:
        backoff = 1.0
        channels = (f"order_book_{self.market}", f"live_trades_{self.market}")
        while True:
            connected_at: float | None = None
            try:
                async with self._connector(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                    connected_at = time.monotonic()
                    for channel in channels:
                        await ws.send(
                            json.dumps({"event": "bts:subscribe", "data": {"channel": channel}})
                        )
                    async for message in ws:
                        received_ms = time.time_ns() // 1_000_000
                        for event in self.parser.parse(message, received_ms):
                            yield event
                    log.warning("bitstamp feed closed; reconnecting")
            except (WebSocketException, OSError, TimeoutError) as exc:
                log.warning("bitstamp feed reconnecting after %s", type(exc).__name__)
            finally:
                self.reconnects += 1
                if connected_at is not None and time.monotonic() - connected_at >= 60:
                    backoff = 1.0
            await asyncio.sleep(backoff * (0.5 + random.random()))  # noqa: S311
            backoff = min(backoff * 2, MAX_BACKOFF_S)
