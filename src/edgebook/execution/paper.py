"""Conservative paper broker using real quoted entry costs and Kalshi fee math."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.execution.models import PaperPosition, SessionState, SessionStatus, SettledPaperTrade


class PaperRiskError(RuntimeError):
    pass


class PaperBroker:
    """Immediate-fill simulator. Settlement must be supplied from the real market result."""

    def __init__(self, state: SessionState) -> None:
        self.state = state

    def submit(
        self,
        *,
        ticker: str,
        side: str,
        yes_price: Decimal,
        fair_probability: Decimal,
        edge: Decimal,
        contracts: Decimal = Decimal("1"),
        now: datetime | None = None,
    ) -> PaperPosition:
        if self.state.status is not SessionStatus.RUNNING:
            raise PaperRiskError(f"session is {self.state.status}")
        if side not in {"YES", "NO"}:
            raise PaperRiskError("side must be YES or NO")
        if edge < self.state.config.min_edge:
            raise PaperRiskError("edge is below the configured threshold")
        if len(self.state.positions) >= self.state.config.max_open_positions:
            raise PaperRiskError("maximum open positions reached")
        if any(position.ticker == ticker for position in self.state.positions):
            raise PaperRiskError("position already open for ticker")
        entry = yes_price if side == "YES" else Decimal("1") - yes_price
        fee = trade_fee(contracts, yes_price, LiquidityRole.TAKER)
        debit = entry * contracts + fee
        if debit > self.state.config.max_order_debit:
            raise PaperRiskError("order exceeds the configured per-order debit")
        if self.state.paper_reserved + debit > self.state.config.max_open_debit:
            raise PaperRiskError("order exceeds the configured open-debit cap")
        if debit > self.state.paper_cash:
            raise PaperRiskError("paper cash is insufficient")
        opened_at = now or datetime.now(UTC)
        position = PaperPosition(
            order_id=str(uuid4()),
            ticker=ticker,
            side=side,
            contracts=contracts,
            entry_cost=entry,
            fee=fee,
            debit=debit,
            fair_probability=fair_probability,
            edge=edge,
            opened_at=opened_at,
        )
        self.state = self.state.model_copy(
            update={
                "paper_cash": self.state.paper_cash - debit,
                "paper_fees": self.state.paper_fees + fee,
                "positions": (*self.state.positions, position),
                "updated_at": opened_at,
                "revision": self.state.revision + 1,
            }
        )
        return position

    def settle(self, ticker: str, result: str, now: datetime | None = None) -> SettledPaperTrade:
        position = next((p for p in self.state.positions if p.ticker == ticker), None)
        if position is None:
            raise PaperRiskError("no open position for ticker")
        won = (position.side == "YES" and result == "yes") or (
            position.side == "NO" and result == "no"
        )
        payout = position.contracts if won else Decimal("0")
        pnl = payout - position.debit
        settled_at = now or datetime.now(UTC)
        settled = SettledPaperTrade(
            **position.model_dump(), settled_at=settled_at, result=result, payout=payout, pnl=pnl
        )
        cash = self.state.paper_cash + payout
        remaining = sum((p.debit for p in self.state.positions if p.ticker != ticker), Decimal("0"))
        equity = cash + remaining
        peak = max(self.state.paper_peak_equity, equity)
        self.state = self.state.model_copy(
            update={
                "paper_cash": cash,
                "paper_realized_pnl": self.state.paper_realized_pnl + pnl,
                "paper_peak_equity": peak,
                "paper_max_drawdown": max(self.state.paper_max_drawdown, peak - equity),
                "positions": tuple(p for p in self.state.positions if p.ticker != ticker),
                "settled": (*self.state.settled, settled),
                "updated_at": settled_at,
                "revision": self.state.revision + 1,
            }
        )
        return settled
