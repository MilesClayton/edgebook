from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from edgebook.core import FeeSchedule, LiquidityRole, edge_per_contract, trade_fee
from edgebook.core.fees import KALSHI_WITH_MAKER_FEES

NON_DIRECT = FeeSchedule(precision=Decimal("0.01"))

prices = st.decimals(min_value=0, max_value=1, places=4, allow_nan=False, allow_infinity=False)
counts = st.integers(min_value=1, max_value=10_000)
roles = st.sampled_from(LiquidityRole)


@pytest.mark.parametrize(
    ("contracts", "price", "role", "schedule", "expected"),
    [
        # BTC series are fee_type "quadratic": takers pay, makers do not.
        (1, "0.50", LiquidityRole.TAKER, None, "0.0175"),
        (1, "0.05", LiquidityRole.TAKER, None, "0.0034"),  # 0.003325 up to 0.01c
        (1, "0.95", LiquidityRole.MAKER, None, "0.0000"),
        (100, "0.50", LiquidityRole.TAKER, None, "1.75"),
        (100, "0.10", LiquidityRole.TAKER, None, "0.63"),
        (100, "0.00", LiquidityRole.TAKER, None, "0.00"),
        (100, "1.00", LiquidityRole.TAKER, None, "0.00"),
        # Series with maker fees, direct-member precision.
        (1, "0.95", LiquidityRole.MAKER, KALSHI_WITH_MAKER_FEES, "0.0009"),  # 0.00083125
        (100, "0.50", LiquidityRole.MAKER, KALSHI_WITH_MAKER_FEES, "0.4375"),
        # Non-direct members are aligned to whole cents.
        (1, "0.50", LiquidityRole.TAKER, NON_DIRECT, "0.02"),
        (1, "0.05", LiquidityRole.TAKER, NON_DIRECT, "0.01"),
    ],
)
def test_kalshi_fee_examples(
    contracts: int,
    price: str,
    role: LiquidityRole,
    schedule: FeeSchedule | None,
    expected: str,
) -> None:
    fee = (
        trade_fee(contracts, Decimal(price), role)
        if schedule is None
        else trade_fee(contracts, Decimal(price), role, schedule)
    )
    assert fee == Decimal(expected)


def test_fee_rounding_doc_example() -> None:
    # docs.kalshi.com/getting_started/fee_rounding: model fee 0.00363825 -> trade fee 0.003639.
    # 0.07 * 0.0577.. is not on the grid, so reproduce it with a multiplier.
    schedule = FeeSchedule(
        multiplier=Decimal("0.00363825") / (Decimal("0.07") * Decimal("0.25")),
        precision=Decimal("0.000001"),
    )
    assert trade_fee(1, Decimal("0.5"), LiquidityRole.TAKER, schedule) == Decimal("0.003639")


@given(counts, prices, roles)
def test_fee_never_negative_and_on_balance_grid(
    contracts: int, price: Decimal, role: LiquidityRole
) -> None:
    for schedule in (FeeSchedule(), KALSHI_WITH_MAKER_FEES, NON_DIRECT):
        fee = trade_fee(contracts, price, role, schedule)
        assert fee >= 0
        assert fee == fee.quantize(schedule.precision)


@given(counts, prices, roles)
def test_direct_never_pays_more_than_non_direct(
    contracts: int, price: Decimal, role: LiquidityRole
) -> None:
    exact = FeeSchedule().coef(role) * contracts * price * (1 - price)
    direct = trade_fee(contracts, price, role)
    assert exact <= direct <= trade_fee(contracts, price, role, NON_DIRECT)
    assert direct - exact < Decimal("0.0001")


@given(counts, prices)
def test_btc_makers_pay_nothing(contracts: int, price: Decimal) -> None:
    assert trade_fee(contracts, price, LiquidityRole.MAKER) == 0


@pytest.mark.parametrize(
    ("fee_type", "multiplier", "direct", "maker", "taker"),
    [
        ("quadratic", 1, True, "0.0000", "0.0175"),
        ("quadratic", 0, True, "0.0000", "0.0000"),
        ("quadratic", "0.5", True, "0.0000", "0.0088"),
        ("quadratic_with_maker_fees", 1, True, "0.0044", "0.0175"),
        ("quadratic_with_combo_maker_fees", 1, True, "0.0088", "0.0175"),
        ("quadratic_with_maker_fees", 1, False, "0.01", "0.02"),
    ],
)
def test_schedule_from_series_fields(
    fee_type: str, multiplier: float | str, direct: bool, maker: str, taker: str
) -> None:
    schedule = FeeSchedule.from_series(fee_type, multiplier, direct_member=direct)
    half = Decimal("0.50")
    assert trade_fee(1, half, LiquidityRole.MAKER, schedule) == Decimal(maker)
    assert trade_fee(1, half, LiquidityRole.TAKER, schedule) == Decimal(taker)


def test_schedule_from_series_rejects_unknown_types() -> None:
    with pytest.raises(ValueError, match="flat"):
        FeeSchedule.from_series("flat", 1)


@given(counts, prices)
def test_maker_never_pays_more_than_taker(contracts: int, price: Decimal) -> None:
    for schedule in (FeeSchedule(), KALSHI_WITH_MAKER_FEES, NON_DIRECT):
        maker = trade_fee(contracts, price, LiquidityRole.MAKER, schedule)
        assert maker <= trade_fee(contracts, price, LiquidityRole.TAKER, schedule)


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
    # Fair 55%, buy at 50c as maker on a BTC series: no fee, the full 5c edge.
    assert edge_per_contract(0.55, Decimal("0.50"), LiquidityRole.MAKER) == Decimal("0.05")
    # On a series with maker fees, 100 contracts: 0.05 - 0.004375 = 0.045625
    edge = edge_per_contract(
        0.55, Decimal("0.50"), LiquidityRole.MAKER, 100, KALSHI_WITH_MAKER_FEES
    )
    assert edge == Decimal("0.045625")
    # A single-contract taker pays the exact 1.75c at direct-member precision ...
    assert edge_per_contract(0.55, Decimal("0.50"), LiquidityRole.TAKER) == Decimal("0.0325")
    # ... and loses 2c to whole-cent alignment without direct membership.
    edge = edge_per_contract(0.55, Decimal("0.50"), LiquidityRole.TAKER, 1, NON_DIRECT)
    assert edge == Decimal("0.03")


@given(st.floats(0, 1), prices, roles)
def test_edge_never_exceeds_fee_free_edge(fair: float, price: Decimal, role: LiquidityRole) -> None:
    assert edge_per_contract(fair, price, role) <= Decimal(str(fair)) - price
