from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from edgebook.core import BookLevel, Market, Order, OrderBook, Side, parse_price
from edgebook.core.types import Action

NOW = datetime(2026, 10, 16, 17, tzinfo=UTC)


def lvl(price: str, size: int = 10) -> BookLevel:
    return BookLevel(price=Decimal(price), size=size)


def test_parse_price_accepts_fixed_point_strings() -> None:
    assert parse_price("0.4300") == Decimal("0.43")
    assert parse_price(Decimal("1")) == 1


@pytest.mark.parametrize("raw", ["-0.01", "1.01", "abc", "NaN"])
def test_parse_price_rejects_out_of_range(raw: str) -> None:
    with pytest.raises(ValueError, match="price"):
        parse_price(raw)


def test_book_derives_ask_from_no_bids() -> None:
    book = OrderBook(
        ticker="KXBTCD-X", ts=NOW, yes_bids=(lvl("0.42"), lvl("0.40")), no_bids=(lvl("0.55"),)
    )
    assert book.best_yes_bid == Decimal("0.42")
    assert book.best_yes_ask == Decimal("0.45")
    assert book.mid == Decimal("0.435")
    assert not book.crossed


def test_empty_side_has_no_mid() -> None:
    book = OrderBook(ticker="KXBTCD-X", ts=NOW, yes_bids=(lvl("0.42"),))
    assert book.best_yes_ask is None
    assert book.mid is None
    assert not book.crossed


def test_crossed_book_detected() -> None:
    book = OrderBook(ticker="T", ts=NOW, yes_bids=(lvl("0.60"),), no_bids=(lvl("0.45"),))
    assert book.crossed


def test_book_levels_must_be_sorted() -> None:
    with pytest.raises(ValidationError, match="best-first"):
        OrderBook(ticker="T", ts=NOW, yes_bids=(lvl("0.40"), lvl("0.42")))


def test_models_are_frozen_and_strict() -> None:
    order = Order(
        client_order_id="c1",
        ticker="T",
        side=Side.YES,
        action=Action.BUY,
        price=Decimal("0.5"),
        count=1,
    )
    with pytest.raises(ValidationError):
        order.count = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Order(
            client_order_id="c1",
            ticker="T",
            side=Side.YES,
            action=Action.BUY,
            price=Decimal("0.5"),
            count=0,
        )


def test_market_is_asset_agnostic() -> None:
    m = Market(
        venue="kalshi",
        ticker="KXETHD-26OCT1617-T4000",
        event_ticker="KXETHD-26OCT1617",
        underlying="ETH",
        strike=Decimal(4000),
        close_time=NOW,
    )
    assert m.underlying == "ETH"
