"""Tick-level replay of recorder data for strategies that live inside a minute.

Replays the recorder's Parquet streams (book snapshots/deltas, public trades,
BRTI index ticks) in arrival order, rebuilding every orderbook, and lets a
``TickStrategy`` act on each event.

Fill model (deliberately conservative):
- taker: fills immediately at the best opposite price if that level has size;
  pays the taker fee.
- maker: a resting buy of YES at p fills only when later public trades print
  YES at <= p with the taker selling YES (taker_side == "no"), and only after
  the volume queued ahead of us at p (the level's size when we joined) has
  traded. Buying NO at q is resting a YES sell at 1 - q, mirrored. Pays the
  maker fee. Unfilled orders expire.
- positions settle at $1/$0 on the market's recorded result; markets without
  a recorded result are reported as open and excluded from P&L.
"""

from __future__ import annotations

import heapq
import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pyarrow.parquet as pq

from edgebook.core.book import BookState
from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole, Side
from edgebook.model.calibration import MeanCI, mean_ci


def fee(price: float, role: LiquidityRole) -> float:
    p = min(max(price, 0.0), 1.0)
    return float(trade_fee(1, Decimal(f"{p:.4f}"), role))


@dataclass
class Fill:
    ticker: str
    side: str  # "yes" | "no": what we hold
    price: float  # paid for that side
    fee: float
    ts_ms: int
    role: str
    tag: str


@dataclass
class RestingOrder:
    ticker: str
    side: str
    price: float  # price for `side`
    queue_ahead: float
    expires_ms: int
    tag: str


@dataclass
class Market:
    ticker: str
    close_ts: int
    floor: float | None
    cap: float | None
    strike_type: str
    result: str = ""


class Context:
    """What a strategy can see and do."""

    def __init__(self, markets: dict[str, Market]) -> None:
        self.markets = markets
        self.books: dict[str, BookState] = {}
        self.now_ms = 0
        self.index_value: float | None = None
        self.index_avg60: float | None = None
        self.index_hist: list[tuple[int, float]] = []
        self.fills: list[Fill] = []
        self.exits: list[Fill] = []  # closing trades (sell back before settlement)
        self.resting: list[RestingOrder] = []
        self.position: dict[str, list[Fill]] = defaultdict(list)

    # ---- market data ----
    def best(self, ticker: str) -> tuple[float | None, float, float | None, float]:
        """(yes_bid, bid_size, yes_ask, ask_size)."""
        book = self.books.get(ticker)
        if book is None or not book.ready:
            return None, 0.0, None, 0.0
        bid = book.best(Side.YES)
        no = book.best(Side.NO)
        return (
            float(bid[0]) if bid else None,
            float(bid[1]) if bid else 0.0,
            float(1 - no[0]) if no else None,
            float(no[1]) if no else 0.0,
        )

    def index_at(self, secs_ago: float) -> float | None:
        target = self.now_ms - secs_ago * 1000
        hist = self.index_hist
        lo, hi = 0, len(hist) - 1
        if not hist or hist[0][0] > target:
            return None
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if hist[mid][0] <= target:
                lo = mid
            else:
                hi = mid - 1
        return hist[lo][1]

    # ---- orders ----
    def take(self, ticker: str, side: str, tag: str) -> Fill | None:
        bid, bsz, ask, asz = self.best(ticker)
        price, size = (ask, asz) if side == "yes" else ((1 - bid) if bid is not None else None, bsz)
        if price is None or size <= 0 or not 0 < price < 1:
            return None
        f = Fill(ticker, side, price, fee(price, LiquidityRole.TAKER), self.now_ms, "taker", tag)
        self.fills.append(f)
        self.position[ticker].append(f)
        return f

    def sell(self, f: Fill, tag: str) -> bool:
        """Close a held contract as a taker at the current bid for its side."""
        bid, bsz, ask, asz = self.best(f.ticker)
        price = bid if f.side == "yes" else ((1 - ask) if ask is not None else None)
        size = bsz if f.side == "yes" else asz
        if price is None or size <= 0:
            return False
        self.position[f.ticker].remove(f)
        self.exits.append(
            Fill(
                f.ticker, f.side, price, fee(price, LiquidityRole.TAKER), self.now_ms, "taker", tag
            )
        )
        return True

    def rest(self, ticker: str, side: str, price: float, ttl_s: float, tag: str) -> None:
        book = self.books.get(ticker)
        queue = 0.0
        if book is not None and book.ready:
            # Decimal("0.45") == Decimal("0.4500") and they hash equal, so either key works.
            queue = float(
                book.size_at(Side.YES if side == "yes" else Side.NO, Decimal(f"{price:.4f}"))
            )
        self.resting.append(
            RestingOrder(ticker, side, price, queue, self.now_ms + int(ttl_s * 1000), tag)
        )

    def keep_or_rest(self, ticker: str, side: str, price: float, ttl_s: float, tag: str) -> None:
        """Quote ``price``: extend an identical resting order (keeping queue priority),
        otherwise cancel this ticker/side/tag's orders and join the back of the queue."""
        expires = self.now_ms + int(ttl_s * 1000)
        mine = [o for o in self.resting if (o.ticker, o.side, o.tag) == (ticker, side, tag)]
        same = [o for o in mine if abs(o.price - price) < 1e-9]
        if same:
            for o in same:
                o.expires_ms = expires
            return
        self.resting = [o for o in self.resting if o not in mine]
        self.rest(ticker, side, price, ttl_s, tag)

    def cancel_all(self, ticker: str | None = None) -> None:
        self.resting = [o for o in self.resting if ticker is not None and o.ticker != ticker]


class TickStrategy(Protocol):
    name: str

    def on_index(self, ctx: Context) -> None: ...
    def on_book(self, ctx: Context, ticker: str) -> None: ...


# ---------------------------------------------------------------- data loading


def load_markets(directory: Path) -> dict[str, Market]:
    out: dict[str, Market] = {}
    for stream in ("markets", "settlements"):
        path = directory / stream
        if not path.exists():
            continue
        for r in pq.read_table(path).to_pylist():
            m = out.get(r["ticker"]) or Market(
                r["ticker"],
                int(r["close_time"].timestamp()),
                float(r["floor_strike"]) if r["floor_strike"] is not None else None,
                float(r["cap_strike"]) if r["cap_strike"] is not None else None,
                r["strike_type"] or "",
            )
            if r.get("result"):
                m.result = r["result"]
            out[r["ticker"]] = m
    return out


def _events(directory: Path) -> Iterator[tuple[int, int, str, Any]]:
    """(recv_ms, order, kind, payload) merged across streams by arrival time."""
    book = pq.read_table(directory / "book").sort_by([("recv_ts_ms", "ascending")]).to_pylist()
    trades = pq.read_table(directory / "trades").sort_by([("recv_ts_ms", "ascending")]).to_pylist()
    index = pq.read_table(directory / "index").sort_by([("recv_ts_ms", "ascending")]).to_pylist()
    streams = [
        ((r["recv_ts_ms"], i, "book", r) for i, r in enumerate(book)),
        ((r["recv_ts_ms"], i, "trade", r) for i, r in enumerate(trades)),
        ((r["recv_ts_ms"], i, "index", r) for i, r in enumerate(index)),
    ]
    yield from heapq.merge(*streams, key=lambda e: (e[0], e[1]))


# ---------------------------------------------------------------- replay


@dataclass
class TickResult:
    name: str
    trades: int
    settled: int
    open_positions: int
    pnl: MeanCI
    by_tag: dict[str, MeanCI] = field(default_factory=dict)
    maker_fills: int = 0


def _match_resting(ctx: Context, trade: dict[str, Any]) -> None:
    yes_px = float(trade["yes_price"])
    qty = float(trade["count"])
    still = []
    for o in ctx.resting:
        if o.expires_ms < ctx.now_ms:
            continue
        if o.ticker != trade["ticker"]:
            still.append(o)
            continue
        # Resting YES buy at p fills on YES-seller takers printing <= p.
        hit = (o.side == "yes" and trade["taker_side"] == "no" and yes_px <= o.price) or (
            o.side == "no" and trade["taker_side"] == "yes" and 1 - yes_px <= o.price
        )
        if not hit:
            still.append(o)
            continue
        if o.queue_ahead > 0:
            o.queue_ahead -= qty
            still.append(o)
            continue
        f = Fill(
            o.ticker, o.side, o.price, fee(o.price, LiquidityRole.MAKER), ctx.now_ms, "maker", o.tag
        )
        ctx.fills.append(f)
        ctx.position[o.ticker].append(f)
    ctx.resting = still


def replay(directory: Path, strategy: TickStrategy) -> TickResult:
    ctx = Context(load_markets(directory))
    pending: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for recv_ms, _, kind, r in _events(directory):
        ctx.now_ms = recv_ms
        if kind == "index":
            raw = json.loads(r["raw"])
            ctx.index_value = float(raw["value"])
            ctx.index_avg60 = float(r["avg_60s"])
            ctx.index_hist.append((recv_ms, ctx.index_value))
            strategy.on_index(ctx)
        elif kind == "trade":
            _match_resting(ctx, r)
        else:
            tk = r["ticker"]
            book = ctx.books.setdefault(tk, BookState(tk))
            if r["kind"] == "reset":
                book.reset()
                book.apply_snapshot([], [])
            elif r["kind"] == "level":
                book.apply_delta(Side(r["side"]), r["price"], r["qty"])
            else:
                try:
                    book.apply_delta(Side(r["side"]), r["price"], r["qty"])
                except (RuntimeError, ValueError):
                    pending[tk].append(r)  # out-of-order; ignored
                    continue
            strategy.on_book(ctx, tk)
    return _score(strategy.name, ctx)


def _score(name: str, ctx: Context) -> TickResult:
    pnls: list[float] = []
    tags: dict[str, list[float]] = defaultdict(list)
    exited = {(e.ticker, e.side) for e in ctx.exits}
    exits = list(ctx.exits)
    open_pos = 0
    for f in ctx.fills:
        held = f in ctx.position[f.ticker]
        if not held:
            ex = next((e for e in exits if e.ticker == f.ticker and e.side == f.side), None)
            if ex is None:
                continue
            exits.remove(ex)
            pnl = ex.price - ex.fee - f.price - f.fee
        else:
            m = ctx.markets.get(f.ticker)
            if m is None or m.result not in ("yes", "no"):
                open_pos += 1
                continue
            won = (m.result == "yes") == (f.side == "yes")
            pnl = (1.0 if won else 0.0) - f.price - f.fee
        pnls.append(pnl)
        tags[f.tag].append(pnl)
    _ = exited
    return TickResult(
        name=name,
        trades=len(ctx.fills),
        settled=len(pnls),
        open_positions=open_pos,
        pnl=mean_ci(np.array(pnls, dtype=np.float64)),
        by_tag={k: mean_ci(np.array(v, dtype=np.float64)) for k, v in tags.items()},
        maker_fills=sum(1 for f in ctx.fills if f.role == "maker"),
    )
