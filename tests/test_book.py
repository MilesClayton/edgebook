from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from edgebook.core import Side
from edgebook.core.book import BookState

D = Decimal
NOW = datetime(2026, 10, 16, tzinfo=UTC)


def snap(book: BookState) -> None:
    book.apply_snapshot(
        yes=[(D("0.08"), D("300")), (D("0.22"), D("333"))],
        no=[(D("0.54"), D("20")), (D("0.56"), D("146")), (D("0.10"), D("0"))],
    )


def test_snapshot_sorts_and_drops_empty_levels() -> None:
    book = BookState("T")
    snap(book)
    ob = book.freeze(NOW)
    assert [lvl.price for lvl in ob.yes_bids] == [D("0.22"), D("0.08")]
    assert [lvl.price for lvl in ob.no_bids] == [D("0.56"), D("0.54")]
    assert ob.best_yes_ask == D("0.44")


def test_deltas_add_remove_levels() -> None:
    book = BookState("T")
    snap(book)
    book.apply_delta(Side.YES, D("0.22"), D("-333"))
    book.apply_delta(Side.YES, D("0.30"), D("5.5"))
    ob = book.freeze(NOW)
    assert [(lvl.price, lvl.size) for lvl in ob.yes_bids] == [
        (D("0.30"), D("5.5")),
        (D("0.08"), D("300")),
    ]


def test_delta_before_snapshot_and_negative_size_rejected() -> None:
    book = BookState("T")
    with pytest.raises(RuntimeError, match="before snapshot"):
        book.apply_delta(Side.YES, D("0.5"), D(1))
    snap(book)
    with pytest.raises(ValueError, match="negative"):
        book.apply_delta(Side.NO, D("0.54"), D(-21))
    book.reset()
    assert not book.ready


@given(
    st.lists(st.tuples(st.sampled_from(Side), st.integers(1, 99), st.integers(1, 50)), max_size=50)
)
def test_adding_then_removing_restores_empty_book(adds: list[tuple[Side, int, int]]) -> None:
    book = BookState("T")
    book.apply_snapshot([], [])
    for side, cents, qty in adds:
        book.apply_delta(side, D(cents) / 100, D(qty))
    for side, cents, qty in reversed(adds):
        book.apply_delta(side, D(cents) / 100, D(-qty))
    ob = book.freeze(NOW)
    assert ob.yes_bids == ()
    assert ob.no_bids == ()
