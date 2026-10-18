"""Strict execution contracts and exact fixed-point accounting units."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DOLLAR = 100_000_000
CENT = DOLLAR // 100


class GuardError(RuntimeError):
    pass


def money(value: str | Decimal) -> int:
    amount = Decimal(value) * DOLLAR
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise GuardError("invalid money precision")
    return int(amount)


def dollars(value: int) -> str:
    return f"{Decimal(value) / DOLLAR:.8f}"


def quantity(value: str) -> int:
    amount = Decimal(value) * 100
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise GuardError("invalid quantity precision")
    return int(amount)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Intent(StrictModel):
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{1,99}$")  # hourly strikes contain "."
    action: Literal["buy", "sell"]
    side: Literal["yes", "no"]
    quantity: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,4})\.[0-9]{2}$")
    limit_price: str = Field(pattern=r"^0\.[0-9]{4}$")

    @property
    def units(self) -> int:
        return quantity(self.quantity)

    @property
    def price(self) -> int:
        return money(self.limit_price)

    @property
    def book_side(self) -> str:
        return "bid" if (self.action == "buy") == (self.side == "yes") else "ask"

    @property
    def yes_price(self) -> int:
        return self.price if self.side == "yes" else DOLLAR - self.price


class Limits(StrictModel):
    capital_budget_cents: int | None = Field(default=None, gt=0, le=100_000_000)
    purchase_spend_cents: int = Field(gt=0, le=100_000_000)
    open_exposure_cents: int = Field(gt=0, le=100_000_000)
    realized_loss_cents: int = Field(gt=0, le=100_000_000)
    order_debit_cents: int = Field(gt=0, le=100_000_000)
    max_contracts: int = Field(gt=0, le=10_000)


class LiveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EDGEBOOK_EXECUTION_", frozen=True)

    enabled: bool = False
    environment: Literal["demo", "production"] = "demo"
    topology: Literal["disabled", "single_host_durable_sqlite"] = "disabled"
    durable_mount: str = ""
    host_id: str = ""
    api_token: str = ""
    proposal_token: str = ""
    direct_member: bool = False
    kalshi_key_id: str = ""
    kalshi_private_key: SecretStr | None = None
    kalshi_private_key_path: Path | None = None
    subaccount: int = Field(default=0, ge=0, le=63)
    # Kalshi exchange shard the execution account trades on; 2 = Crypto and Commodities,
    # where every BTC market lives. Fund this shard (or enable auto-rebalancing) at Kalshi.
    exchange_index: int = Field(default=2, ge=0, le=63)
    purchase_spend_cents: int = Field(default=500, gt=0, le=100_000_000)
    open_exposure_cents: int = Field(default=300, gt=0, le=100_000_000)
    realized_loss_cents: int = Field(default=300, gt=0, le=100_000_000)
    order_debit_cents: int = Field(default=100, gt=0, le=100_000_000)
    max_contracts: int = Field(default=1, gt=0, le=10_000)
    min_edge: float = Field(default=0.04, ge=0.01, le=1, allow_inf_nan=False)
    min_model_confidence: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False)
    jev_model: str = "typesafe-ai/jev"
    exit_advantage: float = Field(default=0.005, ge=0, le=1, allow_inf_nan=False)
    latency_buffer: float = Field(default=0.01, ge=0, le=1, allow_inf_nan=False)
    min_seconds_left: int = Field(default=60, ge=10)
    max_seconds_left: int = Field(default=3600, ge=60)
    data_max_age_ms: int = Field(default=3000, ge=100, le=5000)
    account_max_age_ms: int = Field(default=5000, ge=100, le=10000)
    confirmation_ttl_ms: int = Field(default=15000, ge=1000, le=30000)
    series: tuple[str, ...] = ("KXBTC15M", "KXBTCD")
    # Real-money entries stay blocked until this paper strategy proves profitable, unless the
    # owner explicitly waives it (EDGEBOOK_EXECUTION_REQUIRE_PAPER_PROOF=false). The floors keep
    # the thresholds from being weakened while the gate is on.
    require_paper_proof: bool = True
    # Autopilot (off until the owner switches it on): every autopilot_every_s seconds it
    # reviews the open markets of these series, asks Jev, and confirms Jev's enter/exit
    # through the same checks and caps as a manual Buy. It never adds to a held market.
    autopilot_series: tuple[str, ...] = ("KXBTC15M", "KXBTCD")
    autopilot_contracts: str = Field(default="1.00", pattern=r"^(?:0|[1-9][0-9]{0,4})\.[0-9]{2}$")
    autopilot_every_s: int = Field(default=1, ge=1, le=3600)
    jev_decide_url: str = ""  # https://<dashboard>/api/jev-decide (server-to-server)
    # Long-shot pilot (off until the owner switches it on): mirrors the paper "longshot"
    # strategy with real post-only resting orders of longshot_contracts each, never adding to a
    # held market. Every order reserves its worst-case debit under the caps above while it
    # rests; orders also expire at Kalshi longshot_expire_s before close if this host dies.
    longshot_strategy: str = "longshot"
    longshot_contracts: str = Field(default="1.00", pattern=r"^(?:0|[1-9][0-9]{0,4})\.[0-9]{2}$")
    longshot_every_s: int = Field(default=1, ge=1, le=600)
    longshot_max_orders: int = Field(default=5, ge=1, le=100)
    longshot_event_cents: int = Field(default=500, gt=0, le=100_000_000)  # per hourly close
    longshot_expire_s: int = Field(default=300, ge=60, le=3600)
    proof_strategy: str = "anchored"
    proof_min_trades: int = Field(default=200, ge=100)
    proof_min_days: int = Field(default=5, ge=3)

    def limits(self) -> Limits:
        return Limits.model_validate(
            {name: getattr(self, name) for name in Limits.model_fields if hasattr(self, name)}
        )


class ModelDecision(StrictModel):
    action: Literal["enter_yes", "enter_no", "exit", "hold", "skip"]
    size: Literal["small", "medium", "maximum"] = "small"
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    mode: Literal["gateway", "fixture"]
    model: str = Field(min_length=1, max_length=100)
    resolved_model: str | None = Field(default=None, min_length=1, max_length=200)
    latency_ms: int = Field(ge=0, le=120000)


class ReviewRequest(StrictModel):
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{1,99}$")  # hourly strikes contain "."
    quantity: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,4})\.[0-9]{2}$")


class ProposalRequest(StrictModel):
    review_id: str = Field(min_length=32, max_length=64)
    decision: ModelDecision


class ConfirmRequest(StrictModel):
    proposal_id: str = Field(min_length=32, max_length=64)
    confirmation_token: str = Field(min_length=32, max_length=128)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")
    intent: Intent
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("explicit boolean true confirmation required")
        return value


def fee_reserve(units: int) -> int:
    """Bound direct-member fees without relying on accumulator rebates.

    Supported quadratic coefficient <= .07; .01 contract minimum fills;
    each fill adds <= .000001 trade rounding + .0001 balance rounding.
    """
    return (units * 276 + 99) // 100 * 10_000


def debit(intent: Intent) -> int:
    return intent.price * intent.units // 100 + fee_reserve(intent.units)
