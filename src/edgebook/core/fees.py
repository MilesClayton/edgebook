"""Exchange fee math.

Kalshi's general schedule charges ``coef * C * P * (1 - P)`` per trade, rounded
up to the next cent, with coef 0.07 for takers and 0.0175 for makers. The
coefficients live in ``FeeSchedule`` so other venues or schedule changes are
config, not code.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from pydantic import BaseModel, ConfigDict, Field

from edgebook.core.types import LiquidityRole, parse_price

CENT = Decimal("0.01")


class FeeSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    taker_coef: Decimal = Field(default=Decimal("0.07"), ge=0)
    maker_coef: Decimal = Field(default=Decimal("0.0175"), ge=0)

    def coef(self, role: LiquidityRole) -> Decimal:
        return self.taker_coef if role is LiquidityRole.TAKER else self.maker_coef


KALSHI_GENERAL = FeeSchedule()


def trade_fee(
    contracts: int,
    price: Decimal,
    role: LiquidityRole,
    schedule: FeeSchedule = KALSHI_GENERAL,
) -> Decimal:
    """Total fee in dollars for one fill, rounded up to the next cent."""
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    p = parse_price(price)
    raw = schedule.coef(role) * contracts * p * (1 - p)
    return raw.quantize(CENT, rounding=ROUND_CEILING)


def edge_per_contract(
    fair_prob: float,
    price: Decimal,
    role: LiquidityRole,
    contracts: int = 1,
    schedule: FeeSchedule = KALSHI_GENERAL,
) -> Decimal:
    """Expected profit per contract of buying YES at ``price``, after fees.

    Buying NO at q is buying the complement, so callers price NO trades as
    ``edge_per_contract(1 - fair_prob, q, ...)``.
    """
    if not 0.0 <= fair_prob <= 1.0:
        raise ValueError("fair_prob must be in [0, 1]")
    fee = trade_fee(contracts, price, role, schedule)
    return Decimal(str(fair_prob)) - parse_price(price) - fee / contracts
