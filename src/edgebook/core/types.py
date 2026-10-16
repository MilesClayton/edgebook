"""Domain types shared by every venue, strategy and backtest.

Prices are contract prices in dollars on a $1 binary, held as ``Decimal`` so
venue fixed-point strings (e.g. Kalshi's ``"0.4300"``) round-trip exactly.
Probabilities are model outputs and stay ``float``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

ZERO = Decimal(0)
ONE = Decimal(1)


def _check_price(value: Decimal) -> Decimal:
    if not value.is_finite() or not ZERO <= value <= ONE:
        raise ValueError(f"price must be a finite dollar amount in [0, 1], got {value}")
    return value


def _check_prob(value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"probability must be in [0, 1], got {value}")
    return value


Price = Annotated[Decimal, AfterValidator(_check_price)]
Prob = Annotated[float, AfterValidator(_check_prob)]
Contracts = Annotated[int, Field(gt=0)]


def parse_price(raw: str | int | Decimal) -> Decimal:
    """Parse a venue price (dollar string or Decimal) into a validated price."""
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"not a price: {raw!r}") from exc
    return _check_price(value)


class Side(StrEnum):
    YES = "yes"
    NO = "no"


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"


class LiquidityRole(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Market(_Frozen):
    """One binary contract, e.g. "BTC above $112,500 at 17:00 ET"."""

    venue: str
    ticker: str
    event_ticker: str
    underlying: str
    strike: Decimal | None = None
    close_time: datetime


class BookLevel(_Frozen):
    price: Price
    size: Contracts


class OrderBook(_Frozen):
    """Binary book expressed as resting YES bids and NO bids.

    A NO bid at p is a YES ask at 1 - p, which is how Kalshi publishes books.
    Levels are sorted best-first (highest price first).
    """

    ticker: str
    ts: datetime
    yes_bids: tuple[BookLevel, ...] = ()
    no_bids: tuple[BookLevel, ...] = ()

    @model_validator(mode="after")
    def _sorted_best_first(self) -> OrderBook:
        for levels in (self.yes_bids, self.no_bids):
            prices = [lvl.price for lvl in levels]
            if prices != sorted(prices, reverse=True):
                raise ValueError("book levels must be sorted best-first")
        return self

    @property
    def best_yes_bid(self) -> Decimal | None:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_yes_ask(self) -> Decimal | None:
        return ONE - self.no_bids[0].price if self.no_bids else None

    @property
    def mid(self) -> Decimal | None:
        bid, ask = self.best_yes_bid, self.best_yes_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    @property
    def crossed(self) -> bool:
        bid, ask = self.best_yes_bid, self.best_yes_ask
        return bid is not None and ask is not None and bid >= ask


class Order(_Frozen):
    client_order_id: str
    ticker: str
    side: Side
    action: Action
    price: Price
    count: Contracts
    post_only: bool = True


class Fill(_Frozen):
    client_order_id: str
    ticker: str
    side: Side
    action: Action
    price: Price
    count: Contracts
    role: LiquidityRole
    fee: Decimal
    ts: datetime


class Position(_Frozen):
    """Net YES contracts (negative means net NO) and dollars paid for them."""

    ticker: str
    net_yes: int
    cost_basis: Decimal
