"""Mutable order book state rebuilt from venue snapshot + delta streams.

Venue-agnostic: any venue that publishes YES/NO bid ladders as a snapshot
followed by signed size deltas can drive this. ``freeze`` produces the
immutable ``OrderBook`` the strategy and backtest consume.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from edgebook.core.types import BookLevel, OrderBook, Side


class BookState:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self._levels: dict[Side, dict[Decimal, Decimal]] = {Side.YES: {}, Side.NO: {}}
        self.ready = False

    def apply_snapshot(
        self,
        yes: Iterable[tuple[Decimal, Decimal]],
        no: Iterable[tuple[Decimal, Decimal]],
    ) -> None:
        self._levels = {
            Side.YES: {p: q for p, q in yes if q > 0},
            Side.NO: {p: q for p, q in no if q > 0},
        }
        self.ready = True

    def apply_delta(self, side: Side, price: Decimal, delta: Decimal) -> None:
        if not self.ready:
            raise RuntimeError(f"{self.ticker}: delta before snapshot")
        levels = self._levels[side]
        size = levels.get(price, Decimal(0)) + delta
        if size < 0:
            raise ValueError(f"{self.ticker}: {side} level {price} went negative ({size})")
        if size == 0:
            levels.pop(price, None)
        else:
            levels[price] = size

    def best(self, side: Side) -> tuple[Decimal, Decimal] | None:
        """Best (highest) bid level on ``side`` without building an OrderBook."""
        levels = self._levels[side]
        if not levels:
            return None
        price = max(levels)
        return price, levels[price]

    def size_at(self, side: Side, price: Decimal) -> Decimal:
        return self._levels[side].get(price, Decimal(0))

    def reset(self) -> None:
        self._levels = {Side.YES: {}, Side.NO: {}}
        self.ready = False

    def freeze(self, ts: datetime) -> OrderBook:
        def ladder(side: Side) -> tuple[BookLevel, ...]:
            return tuple(
                BookLevel(price=p, size=q)
                for p, q in sorted(self._levels[side].items(), reverse=True)
            )

        return OrderBook(
            ticker=self.ticker, ts=ts, yes_bids=ladder(Side.YES), no_bids=ladder(Side.NO)
        )
