from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from edgebook.core import FeeSchedule, LiquidityRole, edge_per_contract, trade_fee

prices = st.decimals(min_value=0, max_value=1, places=4, allow_nan=False, allow_infinity=False)
counts = st.integers(min_value=1, max_value=10_000)
roles = st.sampled_from(LiquidityRole)


@pytest.mark.parametrize(
    ("contracts", "price", "role", "expected"),
    [
        (1, "0.50", LiquidityRole.TAKER, "0.02"),  # 0.0175 rounds up
        (100, "0.50", LiquidityRole.TAKER, "1.75"),
        (100, "0.50", LiquidityRole.MAKER, "0.44"),  # 0.4375 rounds up
        (100, "0.10", LiquidityRole.TAKER, "0.63"),
        (100, "0.00", LiquidityRole.TAKER, "0.00"),
        (100, "1.00", LiquidityRole.TAKER, "0.00"),
    ],
)
def test_kalshi_fee_examples(
    contracts: int, price: str, role: LiquidityRole, expected: str
) -> None:
    assert trade_fee(contracts, Decimal(price), role) == Decimal(expected)


@given(counts, prices, roles)
def test_fee_never_negative_and_whole_cents(
    contracts: int, price: Decimal, role: LiquidityRole
) -> None:
    fee = trade_fee(contracts, price, role)
    assert fee >= 0
    assert fee == fee.quantize(Decimal("0.01"))


@given(counts, prices)
def test_maker_never_pays_more_than_taker(contracts: int, price: Decimal) -> None:
    assert trade_fee(contracts, price, LiquidityRole.MAKER) <= trade_fee(
        contracts, price, LiquidityRole.TAKER
    )


@given(counts, prices, roles)
def test_fee_symmetric_around_half(contracts: int, price: Decimal, role: LiquidityRole) -> None:
    assert trade_fee(contracts, price, role) == trade_fee(contracts, 1 - price, role)


def test_custom_schedule_is_config() -> None:
    free = FeeSchedule(taker_coef=Decimal(0), maker_coef=Decimal(0))
    assert trade_fee(10, Decimal("0.5"), LiquidityRole.TAKER, free) == 0


def test_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="positive"):
        trade_fee(0, Decimal("0.5"), LiquidityRole.TAKER)
    with pytest.raises(ValueError, match="price"):
        trade_fee(1, Decimal("1.5"), LiquidityRole.TAKER)
    with pytest.raises(ValueError, match="fair_prob"):
        edge_per_contract(1.2, Decimal("0.5"), LiquidityRole.TAKER)


def test_edge_after_fees() -> None:
    # Fair 55%, buy at 50c as maker, 100 contracts: 0.05 - 0.0044 = 0.0456
    edge = edge_per_contract(0.55, Decimal("0.50"), LiquidityRole.MAKER, contracts=100)
    assert edge == Decimal("0.0456")
    # Same trade as a single-contract taker loses 2c of the 5c edge to rounding
    assert edge_per_contract(0.55, Decimal("0.50"), LiquidityRole.TAKER) == Decimal("0.03")


@given(st.floats(0, 1), prices, roles)
def test_edge_never_exceeds_fee_free_edge(fair: float, price: Decimal, role: LiquidityRole) -> None:
    assert edge_per_contract(fair, price, role) <= Decimal(str(fair)) - price
