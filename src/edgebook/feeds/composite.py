"""Build a robust spot view from independent venue books and trades."""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Iterable

from edgebook.feeds.types import (
    BookQuote,
    CompositeSnapshot,
    FeedEvent,
    SpotBar,
    SpotUpdate,
    TradeTick,
    VenueHealth,
)


class CompositeSpot:
    """Median-mid composite with explicit freshness and source diagnostics.

    Prices and sizes use the configured USD spot pairs. Quotes older than
    ``max_stale_ms`` are excluded from the composite and carry no trading signal.
    """

    def __init__(
        self,
        venues: Iterable[str] = ("coinbase", "kraken", "bitstamp"),
        max_stale_ms: int = 2_000,
        flow_window_ms: int = 60_000,
    ) -> None:
        if max_stale_ms <= 0 or flow_window_ms <= 0:
            raise ValueError("freshness windows must be positive")
        self._venues = dict.fromkeys(venues)
        self._quotes: dict[str, BookQuote] = {}
        self._trades: deque[TradeTick] = deque()
        self.max_stale_ms = max_stale_ms
        self.flow_window_ms = flow_window_ms
        self._bar_start_ms: int | None = None
        self._bar_open = 0.0
        self._bar_high = 0.0
        self._bar_low = 0.0
        self._bar_close = 0.0
        self._bar_volume = 0.0
        self._bar_buy_volume = 0.0
        self._bar_sell_volume = 0.0
        self._bar_book_imbalance: float | None = None

    def ingest(self, event: FeedEvent) -> SpotUpdate:
        """Add one normalized venue event and return the latest composite."""
        self._venues.setdefault(event.venue, None)
        if isinstance(event, BookQuote):
            self._quotes[event.venue] = event
        else:
            self._trades.append(event)

        now_ms = event.received_ts_ms
        cutoff = now_ms - self.flow_window_ms
        self._trades = deque(t for t in self._trades if cutoff <= t.received_ts_ms <= now_ms)
        snapshot = self.snapshot(now_ms)
        completed_bar = self._update_bar(snapshot, event)
        return SpotUpdate(snapshot=snapshot, completed_bar=completed_bar)

    def snapshot(self, now_ms: int) -> CompositeSnapshot:
        active = {
            venue: quote
            for venue, quote in self._quotes.items()
            if now_ms - quote.received_ts_ms <= self.max_stale_ms
        }
        health: dict[str, VenueHealth] = {}
        for venue in self._venues:
            quote = self._quotes.get(venue)
            age = None if quote is None else max(0, now_ms - quote.received_ts_ms)
            is_active = quote is not None and venue in active
            health[venue] = VenueHealth(
                active=is_active,
                staleness_ms=age,
                latency_ms=(
                    None if quote is None else max(0, quote.received_ts_ms - quote.event_ts_ms)
                ),
            )

        mids = [(q.bid + q.ask) / 2 for q in active.values()]
        mid = statistics.median(mids) if mids else None
        bid_quote = max(active.values(), key=lambda q: q.bid, default=None)
        ask_quote = min(active.values(), key=lambda q: q.ask, default=None)
        spread_bps = None
        if len(mids) >= 2 and mid is not None and mid > 0:
            spread_bps = (max(mids) - min(mids)) / mid * 10_000

        trade_volume = sum(t.size for t in self._trades)
        signed_volume = sum(t.size if t.side == "buy" else -t.size for t in self._trades)
        flow = signed_volume / trade_volume if trade_volume else 0.0

        imbalances = [
            (q.bid_size - q.ask_size) / (q.bid_size + q.ask_size)
            for q in active.values()
            if q.bid_size + q.ask_size > 0
        ]
        latencies = [h.latency_ms for h in health.values() if h.active and h.latency_ms is not None]
        return CompositeSnapshot(
            timestamp_ms=now_ms,
            mid=mid,
            bid=None if bid_quote is None else bid_quote.bid,
            bid_size=None if bid_quote is None else bid_quote.bid_size,
            ask=None if ask_quote is None else ask_quote.ask,
            ask_size=None if ask_quote is None else ask_quote.ask_size,
            venues=health,
            cross_exchange_spread_bps=spread_bps,
            trade_flow_imbalance=flow,
            book_imbalance=statistics.fmean(imbalances) if imbalances else None,
            latency_ms=statistics.median(latencies) if latencies else None,
        )

    def _update_bar(self, snapshot: CompositeSnapshot, event: FeedEvent) -> SpotBar | None:
        if snapshot.mid is None:
            return None
        start_ms = event.received_ts_ms // 1000 * 1000
        completed = None
        if self._bar_start_ms is None or start_ms > self._bar_start_ms:
            if self._bar_start_ms is not None:
                completed = self._finish_bar()
            self._start_bar(start_ms, snapshot.mid, snapshot.book_imbalance)
        elif start_ms < self._bar_start_ms:
            return None
        else:
            self._bar_high = max(self._bar_high, snapshot.mid)
            self._bar_low = min(self._bar_low, snapshot.mid)
            self._bar_close = snapshot.mid
            self._bar_book_imbalance = snapshot.book_imbalance

        if isinstance(event, TradeTick):
            self._bar_volume += event.size
            if event.side == "buy":
                self._bar_buy_volume += event.size
            else:
                self._bar_sell_volume += event.size
        return completed

    def _start_bar(self, start_ms: int, mid: float, book_imbalance: float | None) -> None:
        self._bar_start_ms = start_ms
        self._bar_open = self._bar_high = self._bar_low = self._bar_close = mid
        self._bar_volume = self._bar_buy_volume = self._bar_sell_volume = 0.0
        self._bar_book_imbalance = book_imbalance

    def _finish_bar(self) -> SpotBar:
        total = self._bar_buy_volume + self._bar_sell_volume
        return SpotBar(
            start_ms=self._bar_start_ms if self._bar_start_ms is not None else 0,
            open=self._bar_open,
            high=self._bar_high,
            low=self._bar_low,
            close=self._bar_close,
            volume=self._bar_volume,
            buy_volume=self._bar_buy_volume,
            sell_volume=self._bar_sell_volume,
            trade_flow_imbalance=(self._bar_buy_volume - self._bar_sell_volume) / total
            if total
            else 0.0,
            book_imbalance=self._bar_book_imbalance,
        )
