"""Simulated resting (maker) orders for a paper account.

Instead of paying the ask, the maker strategy rests one bid per market below
fair value and waits for a seller. Fills are deliberately conservative and
match the tick-replay research model (``research.tick``):

- a resting UP (YES) bid at q fills only from public trades where the taker
  sold YES (``taker_side == "no"``) at a YES price <= q; a DOWN (NO) bid at q
  fills from takers buying YES at a YES price >= 1 - q;
- a print exactly at our price first uses up the volume that was already
  queued at that price when we joined; a print through our price means every
  bid at our price was taken, so we fill (up to the print's size);
- fills pay Kalshi's maker fee, and the order never crosses the spread.

Orders are re-posted (losing queue priority) when the target price changes,
and cancelled when a data check fails or the market is about to close.
Nothing here places real orders.
"""

from __future__ import annotations

from dataclasses import dataclass

from edgebook.core.types import LiquidityRole
from edgebook.engine.decide import Call, fee
from edgebook.engine.paper import Fill, PaperAccount
from edgebook.engine.state import LiveState

MAKER = LiquidityRole.MAKER


@dataclass(frozen=True)
class MakerConfig:
    edge: float = 0.02  # fair - bid - maker fee, $ per contract


@dataclass
class RestingOrder:
    ticker: str
    side: str  # "yes" (UP) | "no" (DOWN)
    price: float
    contracts: float  # still unfilled
    queue_ahead: float  # contracts at our price that must trade before us
    placed_ms: int
    fair: float  # fair value of `side` when placed


def maker_bid(
    fair_side: float, best_bid: float | None, best_ask: float | None, edge: float
) -> float | None:
    """Highest whole-cent bid q with fair - q - maker_fee(q) >= edge, never more than
    one cent above the best bid and always below the best ask (None if no such q)."""
    cap = 0.99
    if best_bid is not None:
        cap = min(cap, best_bid + 0.01)
    if best_ask is not None:
        cap = min(cap, best_ask - 0.01)
    best = None
    for cents in range(1, round(cap * 100) + 1):
        q = cents / 100
        if fair_side - q - fee(q, MAKER) >= edge - 1e-9:
            best = q
    return best


class MakerBook:
    """One account's resting orders (at most one per market)."""

    def __init__(self) -> None:
        self.orders: dict[str, RestingOrder] = {}
        self.trade_seq = 0

    def match(self, state: LiveState, account: PaperAccount) -> list[Fill]:
        """Fill resting orders from the trade prints that arrived since the last call."""
        fills: list[Fill] = []
        for t in state.trades_after(self.trade_seq):
            self.trade_seq = t.seq
            o = self.orders.get(t.ticker)
            if o is None:
                continue
            side_px = t.yes_price if o.side == "yes" else 1 - t.yes_price
            seller_hit_us = t.taker_side == ("no" if o.side == "yes" else "yes")
            if not seller_hit_us or side_px > o.price + 1e-9:
                continue
            qty = t.count
            if side_px < o.price - 1e-9:
                o.queue_ahead = 0.0  # traded through our price: everyone at it was filled
            elif o.queue_ahead > 0:
                used = min(o.queue_ahead, qty)
                o.queue_ahead -= used
                qty -= used
            take = min(qty, o.contracts)
            if take <= 0:
                continue
            f = account.add_fill(
                o.ticker,
                o.side,
                take,
                o.price,
                fee(o.price, MAKER),
                t.ts_ms,
                f"Resting {'UP' if o.side == 'yes' else 'DOWN'} bid at {o.price:.2f} filled "
                f"(fair value {o.fair:.2f}).",
            )
            if f is None:
                del self.orders[o.ticker]
                continue
            fills.append(f)
            o.contracts -= take
            if o.contracts <= 1e-9:
                del self.orders[o.ticker]
        return fills

    def cancel(self, ticker: str) -> None:
        self.orders.pop(ticker, None)

    def quote(
        self,
        state: LiveState,
        account: PaperAccount,
        call: Call,
        cfg: MakerConfig,
        equity: float,
        now_ms: int,
        allowed: bool,
    ) -> RestingOrder | None:
        """Place, keep or cancel this market's resting bid from a fresh call."""
        tk = call.ticker
        o = self.orders.get(tk)
        held = account.holdings.get(tk)
        if held is not None:
            # keep working the order that opened this position; never open a second side
            if not allowed or o is None or o.side != held.side:
                self.cancel(tk)
                return None
            return o
        if not allowed or call.fair_up is None or not account.can_enter(tk, now_ms):
            self.cancel(tk)
            return None
        choices = []
        for side, fair_side, bid, ask in (
            ("yes", call.fair_up, call.up_sell, call.up_price),
            ("no", 1 - call.fair_up, call.down_sell, call.down_price),
        ):
            q = maker_bid(fair_side, bid, ask, cfg.edge)
            if q is not None:
                choices.append((fair_side - q - fee(q, MAKER), side, q, fair_side))
        if not choices:
            self.cancel(tk)
            return None
        _, side, q, fair_side = max(choices)
        if o is not None and o.side == side and abs(o.price - q) < 1e-9:
            return o  # unchanged: keep our place in the queue
        contracts = account.budget(equity) // (q + fee(q, MAKER))
        if contracts <= 0:
            self.cancel(tk)
            return None
        queue = sum(lv.size for lv in state.bids(tk, side) if abs(lv.price - q) < 1e-9)
        self.orders[tk] = RestingOrder(tk, side, q, contracts, queue, now_ms, fair_side)
        return self.orders[tk]


# ---------------------------------------------------------------- long-shot seller


@dataclass(frozen=True)
class LongshotConfig:
    """Sell overpriced long shots as a maker (rest a DOWN bid that joins the UP ask).

    Evidence (research.longshot, 30-day backfill + recorded trades): YES contracts
    asked at 2-15c win less often than their price, and makers selling them
    earned roughly +1 to +4c per contract after fees.
    """

    min_ask: float = 0.02  # UP (YES) ask band that counts as a long shot
    max_ask: float = 0.15
    min_secs_left: float = 300.0
    max_secs_left: float = 3600.0
    max_event_cost: float = 15_000.0  # all strikes of one close move together: cap the sum


def longshot_quote(call: Call, cfg: LongshotConfig) -> tuple[str, float, float] | None:
    """(side, bid price, fair of that side) for selling this market's long shot, or None.

    Sells UP when UP is the long shot (rest a DOWN bid at 1 - UP ask), or DOWN when
    DOWN is (rest an UP bid at 1 - DOWN ask). Never sells a long shot that fair value
    says is worth at least its ask.
    """
    if not cfg.min_secs_left <= call.seconds_left <= cfg.max_secs_left:
        return None
    for long_ask, fair_long, our_side in (
        (call.up_price, call.fair_up, "no"),
        (call.down_price, None if call.fair_up is None else 1 - call.fair_up, "yes"),
    ):
        if long_ask is None or not cfg.min_ask <= long_ask <= cfg.max_ask:
            continue
        if fair_long is not None and fair_long >= long_ask:
            return None  # fair value says this long shot is not overpriced
        q = round(1 - long_ask, 2)
        return our_side, q, 1 - (fair_long if fair_long is not None else long_ask)
    return None


def quote_longshot(
    book: MakerBook,
    state: LiveState,
    account: PaperAccount,
    call: Call,
    cfg: LongshotConfig,
    equity: float,
    now_ms: int,
    allowed: bool,
    event_room: float,
) -> RestingOrder | None:
    """Place, keep or cancel this market's long-shot sale (a resting bid on the favourite)."""
    tk = call.ticker
    o = book.orders.get(tk)
    held = account.holdings.get(tk)
    if held is not None:
        if not allowed or o is None or o.side != held.side:
            book.cancel(tk)
            return None
        return o
    q = longshot_quote(call, cfg) if allowed and account.can_enter(tk, now_ms) else None
    if q is None:
        book.cancel(tk)
        return None
    side, price, fair_side = q
    if o is not None and o.side == side and abs(o.price - price) < 1e-9:
        return o  # unchanged: keep our place in the queue
    unit = price + fee(price, MAKER)
    contracts = min(account.budget(equity), max(event_room, 0.0)) // unit
    if contracts <= 0:
        book.cancel(tk)
        return None
    queue = sum(lv.size for lv in state.bids(tk, side) if abs(lv.price - price) < 1e-9)
    book.orders[tk] = RestingOrder(tk, side, price, contracts, queue, now_ms, fair_side)
    return book.orders[tk]
