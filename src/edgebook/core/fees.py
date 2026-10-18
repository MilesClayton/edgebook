"""Exchange fee math.

Kalshi charges ``multiplier * coef * C * P * (1 - P)`` per fill, with coef 0.07 for takers.
Makers pay 0.0175 (0.035 on combo schedules) only on series whose ``fee_type`` includes
maker fees; plain ``quadratic`` series, which include every BTC series edgebook trades
(KXBTC15M, KXBTCD, KXBTC), charge makers nothing.

Rounding (docs.kalshi.com/getting_started/fee_rounding): the model fee is rounded up to
$0.000001, then the balance is aligned to the account's precision: $0.0001 for direct
members, $0.01 for everyone else (FCM and broker customers). Kalshi rebates accumulated
alignment overpayment on later fills of the same order; this module ignores the rebate, so
for whole-cent prices and whole contracts it returns the most a single fill can cost.
Coefficients and precision live in ``FeeSchedule`` so schedule changes are config, not code.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from pydantic import BaseModel, ConfigDict, Field

from edgebook.core.types import LiquidityRole, parse_price

CENT = Decimal("0.01")
DIRECT_MEMBER_PRECISION = Decimal("0.0001")
TRADE_FEE_PRECISION = Decimal("0.000001")
TAKER_COEF = Decimal("0.07")
MAKER_COEF = Decimal("0.0175")  # 0.25 x taker
COMBO_MAKER_COEF = Decimal("0.035")  # 0.5 x taker


class FeeSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    taker_coef: Decimal = Field(default=TAKER_COEF, ge=0)
    maker_coef: Decimal = Field(default=Decimal(0), ge=0)
    multiplier: Decimal = Field(default=Decimal(1), ge=0)
    precision: Decimal = Field(default=DIRECT_MEMBER_PRECISION, gt=0)

    def coef(self, role: LiquidityRole) -> Decimal:
        base = self.taker_coef if role is LiquidityRole.TAKER else self.maker_coef
        return base * self.multiplier

    @classmethod
    def from_series(
        cls, fee_type: str, fee_multiplier: float | str | Decimal, direct_member: bool = True
    ) -> FeeSchedule:
        """Schedule for a Kalshi series' ``fee_type`` / ``fee_multiplier`` (GET /series)."""
        makers = {
            "quadratic": Decimal(0),
            "quadratic_with_maker_fees": MAKER_COEF,
            "quadratic_with_combo_maker_fees": COMBO_MAKER_COEF,
        }
        if fee_type not in makers:
            raise ValueError(f"unsupported fee_type {fee_type!r}")
        return cls(
            maker_coef=makers[fee_type],
            multiplier=Decimal(str(fee_multiplier)),
            precision=DIRECT_MEMBER_PRECISION if direct_member else CENT,
        )


# Every BTC series edgebook trades, for the direct-member account (verified 2026-10-18).
KALSHI_BTC = FeeSchedule()
# A series with maker fees, for comparison and for non-BTC research.
KALSHI_WITH_MAKER_FEES = FeeSchedule(maker_coef=MAKER_COEF)


def trade_fee(
    contracts: Decimal | int,
    price: Decimal,
    role: LiquidityRole,
    schedule: FeeSchedule = KALSHI_BTC,
) -> Decimal:
    """Total fee in dollars for one fill, rounded up to the schedule's balance precision."""
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    p = parse_price(price)
    raw = schedule.coef(role) * contracts * p * (1 - p)
    fee = raw.quantize(TRADE_FEE_PRECISION, rounding=ROUND_CEILING)
    return fee.quantize(schedule.precision, rounding=ROUND_CEILING)


def edge_per_contract(
    fair_prob: float,
    price: Decimal,
    role: LiquidityRole,
    contracts: Decimal | int = 1,
    schedule: FeeSchedule = KALSHI_BTC,
) -> Decimal:
    """Expected profit per contract of buying YES at ``price``, after fees.

    Buying NO at q is buying the complement, so callers price NO trades as
    ``edge_per_contract(1 - fair_prob, q, ...)``.
    """
    if not 0.0 <= fair_prob <= 1.0:
        raise ValueError("fair_prob must be in [0, 1]")
    fee = trade_fee(contracts, price, role, schedule)
    return Decimal(str(fair_prob)) - parse_price(price) - fee / contracts
