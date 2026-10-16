"""Execution state models. Monetary values are Decimal, never float."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ExecutionMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class SessionStatus(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    HALTED = "halted"


class SessionConfig(BaseModel):
    """User-selected bounds enforced by the worker, not by the dashboard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: ExecutionMode = ExecutionMode.PAPER
    paper_starting_cash: Decimal = Field(default=Decimal("25.00"), gt=0)
    max_open_debit: Decimal = Field(default=Decimal("5.00"), gt=0)
    max_order_debit: Decimal = Field(default=Decimal("1.00"), gt=0)
    min_edge: Decimal = Field(default=Decimal("0.02"), ge=0, le=1)
    max_open_positions: int = Field(default=1, ge=1, le=10)


class PaperPosition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    ticker: str
    side: str  # YES | NO
    contracts: Decimal
    entry_cost: Decimal
    fee: Decimal
    debit: Decimal
    fair_probability: Decimal
    edge: Decimal
    opened_at: datetime


class SettledPaperTrade(PaperPosition):
    settled_at: datetime
    result: str
    payout: Decimal
    pnl: Decimal


class SessionState(BaseModel):
    """Materialized state; every transition is also written to the event log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    revision: int = 0
    status: SessionStatus = SessionStatus.STOPPED
    config: SessionConfig
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    paper_cash: Decimal
    paper_realized_pnl: Decimal = Decimal("0")
    paper_fees: Decimal = Decimal("0")
    paper_peak_equity: Decimal
    paper_max_drawdown: Decimal = Decimal("0")
    positions: tuple[PaperPosition, ...] = ()
    settled: tuple[SettledPaperTrade, ...] = ()
    halt_reason: str | None = None

    @property
    def paper_reserved(self) -> Decimal:
        return sum((position.debit for position in self.positions), Decimal("0"))

    @property
    def paper_equity(self) -> Decimal:
        return self.paper_cash + self.paper_reserved
