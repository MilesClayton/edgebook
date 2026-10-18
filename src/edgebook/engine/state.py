"""Everything the engine knows right now, kept current by the recorder's stream.

Sources and why:
- Kalshi order books: rebuilt from the WebSocket snapshot + delta stream.
- CF Benchmarks BRTI (the settlement index), ~1 tick/s from Kalshi's
  ``cfbenchmarks_value`` channel. This, not an exchange price, decides results.
- Kalshi public trades: kept briefly so simulated resting (maker) orders can
  fill only when real volume prints through their price.
- Optional multi-exchange composite (Coinbase/Kraken/Bitstamp): used only to
  detect that spot is moving faster than the index (a lag warning).
"""

from __future__ import annotations

import itertools
import math
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from edgebook.core.book import BookState
from edgebook.core.types import Side
from edgebook.feeds.types import CompositeSnapshot
from edgebook.venues.kalshi.models import (
    DataMessage,
    IndexValue,
    OrderbookDelta,
    OrderbookSnapshot,
    RestMarket,
    Trade,
)

INDEX_KEEP_S = 1800
TRADES_KEPT = 5000


@dataclass
class MarketInfo:
    ticker: str
    series: str
    open_ts: int
    close_ts: int
    strike_type: str
    floor: float | None
    cap: float | None
    title: str = ""


@dataclass(frozen=True)
class TradePrint:
    seq: int  # increasing arrival number, so consumers can resume where they left off
    ticker: str
    yes_price: float
    count: float
    taker_side: str  # "yes": taker bought YES (sold NO); "no": taker sold YES
    ts_ms: int


@dataclass(frozen=True)
class Level:
    price: float  # dollars for the side being traded
    size: float  # contracts


class LiveState:
    def __init__(self, index_id: str = "BRTI") -> None:
        self.index_id = index_id
        self.books: dict[str, BookState] = {}
        self.markets: dict[str, MarketInfo] = {}
        self.index: deque[tuple[int, float]] = deque()
        self.ws_last_ms = 0
        self.composite: CompositeSnapshot | None = None
        self.composite_ms = 0
        self.trades: deque[TradePrint] = deque(maxlen=TRADES_KEPT)
        self._sigma_key: object = None
        self._sigma: float | None = None
        self._trade_seq = 0

    # ---- updates -------------------------------------------------------
    def set_market(self, m: RestMarket, series: str) -> None:
        close_ts = int(m.close_time.timestamp())
        open_ts = int(m.open_time.timestamp()) if m.open_time else close_ts - 3600
        self.markets[m.ticker] = MarketInfo(
            ticker=m.ticker,
            series=series,
            open_ts=open_ts,
            close_ts=close_ts,
            strike_type=m.strike_type or "",
            floor=float(m.floor_strike) if m.floor_strike is not None else None,
            cap=float(m.cap_strike) if m.cap_strike is not None else None,
        )

    def on_message(self, msg: DataMessage, recv_ms: int) -> None:
        self.ws_last_ms = recv_ms
        if isinstance(msg, OrderbookSnapshot):
            book = self.books.setdefault(msg.msg.market_ticker, BookState(msg.msg.market_ticker))
            book.apply_snapshot(msg.msg.yes_dollars_fp, msg.msg.no_dollars_fp)
        elif isinstance(msg, OrderbookDelta):
            existing = self.books.get(msg.msg.market_ticker)
            if existing is not None and existing.ready:
                book = existing
                try:
                    book.apply_delta(msg.msg.side, msg.msg.price_dollars, msg.msg.delta_fp)
                except ValueError:
                    book.reset()  # inconsistent; the ws client resyncs on sequence gaps
        elif isinstance(msg, Trade):
            self.add_trade(
                msg.msg.market_ticker,
                float(msg.msg.yes_price_dollars),
                float(msg.msg.count_fp),
                msg.msg.taker_side.value,
                recv_ms,
            )
        elif isinstance(msg, IndexValue) and msg.msg.index_id == self.index_id:
            raw = msg.msg.data
            value = _index_value(raw)
            if value is not None:
                self.index.append((recv_ms, value))
                while self.index and self.index[0][0] < recv_ms - INDEX_KEEP_S * 1000:
                    self.index.popleft()

    def add_trade(
        self, ticker: str, yes_price: float, count: float, taker_side: str, ts_ms: int
    ) -> None:
        self._trade_seq += 1
        self.trades.append(TradePrint(self._trade_seq, ticker, yes_price, count, taker_side, ts_ms))

    def trades_after(self, seq: int) -> list[TradePrint]:
        return [t for t in self.trades if t.seq > seq]

    def set_composite(self, snap: CompositeSnapshot, recv_ms: int) -> None:
        self.composite = snap
        self.composite_ms = recv_ms

    # ---- reads ---------------------------------------------------------
    def index_now(self) -> tuple[int, float] | None:
        return self.index[-1] if self.index else None

    def history_s(self, now_ms: int) -> float:
        """Seconds of index history available (from the first tick to now)."""
        return (now_ms - self.index[0][0]) / 1000 if self.index else 0.0

    def index_avg_since(self, start_ms: int) -> tuple[float, int] | None:
        """Average and count of index prints at or after ``start_ms``."""
        vals = [v for ts, v in self.index if ts >= start_ms]
        return (sum(vals) / len(vals), len(vals)) if vals else None

    def sigma_per_s(self, now_ms: int, halflife_s: float = 300.0) -> float | None:
        """EWMA per-second log-vol of the index from ~1 Hz prints (needs 2+ minutes).

        Called for every market, every strategy, every tick; the answer only changes when
        the clock or the index changes, so it is computed once per (time, last print)."""
        last = self.index[-1] if self.index else None
        key = (now_ms, halflife_s, last, len(self.index))
        if key != self._sigma_key:
            self._sigma_key, self._sigma = key, self._sigma_uncached(now_ms, halflife_s)
        return self._sigma

    def _sigma_uncached(self, now_ms: int, halflife_s: float) -> float | None:
        pts = [(ts, v) for ts, v in self.index if ts >= now_ms - 1800 * 1000]
        if len(pts) < 120:
            return None
        num = den = 0.0
        for (t0, v0), (t1, v1) in itertools.pairwise(pts):
            dt = max((t1 - t0) / 1000, 0.2)
            r2 = math.log(v1 / v0) ** 2 / dt
            w = 0.5 ** ((now_ms - t1) / 1000 / halflife_s)
            num += w * r2
            den += w
        return math.sqrt(num / den) if den > 0 else None

    def flow(self, ticker: str, now_ms: int, levels: int = 5) -> dict[str, Any]:
        """What a pro trading screen shows for one market: the trade tape (who is buying),
        order-book depth beyond the top level, and how fast the index is moving.

        Context for Jev's decision only. The engine's prices and gates do not use it, so
        it can make Jev skip a trade but never create one."""
        tape: dict[str, Any] = {}
        for label, window_s in (("last_60s", 60), ("last_5m", 300)):
            prints = [
                t for t in self.trades if t.ticker == ticker and t.ts_ms >= now_ms - window_s * 1000
            ]
            up = sum(t.count for t in prints if t.taker_side == "yes")
            volume = up + sum(t.count for t in prints if t.taker_side == "no")
            tape[label] = {
                "trades": len(prints),
                "contracts": round(volume, 2),
                "share_bought_up": round(up / volume, 3) if volume else None,
                "avg_up_price": round(sum(t.yes_price * t.count for t in prints) / volume, 4)
                if volume
                else None,
            }
        last = next((t for t in reversed(self.trades) if t.ticker == ticker), None)
        tape["last_trade"] = (
            None
            if last is None
            else {
                "up_price": last.yes_price,
                "contracts": last.count,
                "buyer": "up" if last.taker_side == "yes" else "down",
                "age_s": round((now_ms - last.ts_ms) / 1000, 1),
            }
        )
        book: dict[str, Any] = {
            f"buy_{name}": [
                [round(lv.price, 4), round(lv.size, 2)] for lv in self.asks(ticker, side)[:levels]
            ]
            for name, side in (("up", "yes"), ("down", "no"))
        }
        up_depth = sum(size for _, size in book["buy_up"])
        down_depth = sum(size for _, size in book["buy_down"])
        book["up_share_of_depth"] = (
            round(up_depth / (up_depth + down_depth), 3) if up_depth + down_depth else None
        )
        momentum: dict[str, float | None] = {}
        current = self.index_now()
        for label, back_s in (("index_change_60s_bps", 60), ("index_change_5m_bps", 300)):
            past = next((v for ts, v in self.index if ts >= now_ms - back_s * 1000), None)
            momentum[label] = (
                round((current[1] / past - 1) * 10_000, 2) if current and past else None
            )
        return {
            "tape": tape,
            "book": book,
            "momentum": momentum,
            "note": "Market activity for context only; the engine's prices and gates ignore it.",
        }

    def asks(self, ticker: str, side: str) -> list[Level]:
        """Levels you can BUY ``side`` at, cheapest first (from the opposite bids)."""
        book = self.books.get(ticker)
        if book is None or not book.ready:
            return []
        opp = book._levels[Side.NO if side == "yes" else Side.YES]
        return [Level(float(1 - p), float(q)) for p, q in sorted(opp.items(), reverse=True)]

    def bids(self, ticker: str, side: str) -> list[Level]:
        """Levels you can SELL ``side`` into, best first."""
        book = self.books.get(ticker)
        if book is None or not book.ready:
            return []
        own = book._levels[Side.YES if side == "yes" else Side.NO]
        return [Level(float(p), float(q)) for p, q in sorted(own.items(), reverse=True)]


def _index_value(raw: str) -> float | None:
    import json

    try:
        return float(Decimal(str(json.loads(raw)["value"])))
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return None
