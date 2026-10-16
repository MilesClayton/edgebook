"""Merge independent venue streams into a single composite output stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from edgebook.feeds.bitstamp import BitstampFeed
from edgebook.feeds.coinbase import CoinbaseFeed
from edgebook.feeds.composite import CompositeSpot
from edgebook.feeds.kraken import KrakenFeed
from edgebook.feeds.types import FeedEvent, SpotUpdate


class Feed(Protocol):
    def events(self) -> AsyncIterator[FeedEvent]: ...


class MultiExchangeSpot:
    """Async composite stream backed by Coinbase, Kraken and Bitstamp."""

    def __init__(
        self,
        feeds: Sequence[Feed] | None = None,
        composite: CompositeSpot | None = None,
    ) -> None:
        self.feeds = (
            tuple(feeds)
            if feeds is not None
            else (
                CoinbaseFeed(),
                KrakenFeed(),
                BitstampFeed(),
            )
        )
        self.composite = composite or CompositeSpot()

    async def updates(self) -> AsyncIterator[SpotUpdate]:
        queue: asyncio.Queue[SpotUpdate] = asyncio.Queue()

        async def pump(feed: Feed) -> None:
            async for event in feed.events():
                await queue.put(self.composite.ingest(event))

        async with asyncio.TaskGroup() as group:
            for feed in self.feeds:
                group.create_task(pump(feed))
            while True:
                yield await queue.get()
