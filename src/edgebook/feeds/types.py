"""Normalized spot feed events and composite output types."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

type Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class BookQuote:
    venue: str
    symbol: str
    bid: float
    bid_size: float
    ask: float
    ask_size: float
    event_ts_ms: int
    received_ts_ms: int

    def __post_init__(self) -> None:
        values = (self.bid, self.bid_size, self.ask, self.ask_size)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("book quote values must be finite")
        if self.bid <= 0 or self.ask <= 0 or self.bid >= self.ask:
            raise ValueError("book quote must have positive, non-crossed prices")
        if self.bid_size < 0 or self.ask_size < 0:
            raise ValueError("book sizes cannot be negative")


@dataclass(frozen=True, slots=True)
class TradeTick:
    venue: str
    symbol: str
    side: Side
    price: float
    size: float
    event_ts_ms: int
    received_ts_ms: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.price) or not math.isfinite(self.size):
            raise ValueError("trade values must be finite")
        if self.price <= 0 or self.size <= 0:
            raise ValueError("trade price and size must be positive")


type FeedEvent = BookQuote | TradeTick


@dataclass(frozen=True, slots=True)
class VenueHealth:
    active: bool
    staleness_ms: int | None
    latency_ms: int | None


@dataclass(frozen=True, slots=True)
class CompositeSnapshot:
    timestamp_ms: int
    mid: float | None
    bid: float | None
    bid_size: float | None
    ask: float | None
    ask_size: float | None
    venues: dict[str, VenueHealth]
    cross_exchange_spread_bps: float | None
    trade_flow_imbalance: float
    book_imbalance: float | None
    latency_ms: float | None


@dataclass(frozen=True, slots=True)
class SpotBar:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_volume: float
    sell_volume: float
    trade_flow_imbalance: float
    book_imbalance: float | None


@dataclass(frozen=True, slots=True)
class SpotUpdate:
    snapshot: CompositeSnapshot
    completed_bar: SpotBar | None = None
