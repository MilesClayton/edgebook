"""Stateful paper session stepping against real public Kalshi quotes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from edgebook.execution.models import (
    ExecutionMode,
    PaperPosition,
    SessionConfig,
    SessionState,
    SessionStatus,
)
from edgebook.execution.paper import PaperBroker, PaperRiskError
from edgebook.execution.store import SessionStore
from edgebook.live.signals import LivePricer, Signal, signals
from edgebook.venues.kalshi.rest import KalshiRest


class PaperSession:
    """One durable paper session. It uses public data only and never submits orders."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def start(self, config: SessionConfig) -> SessionState:
        current = self.store.load()
        if current is not None and current.status is SessionStatus.RUNNING:
            raise PaperRiskError("a paper session is already running")
        now = datetime.now(UTC)
        state = SessionState(
            session_id=str(uuid4()),
            status=SessionStatus.RUNNING,
            config=config,
            paper_cash=config.paper_starting_cash,
            paper_peak_equity=config.paper_starting_cash,
            created_at=now,
            updated_at=now,
        )
        self.store.save(state, "session_started", {"mode": config.mode.value})
        return state

    def stop(self) -> SessionState:
        state = self._state()
        now = datetime.now(UTC)
        stopped = state.model_copy(
            update={
                "status": SessionStatus.STOPPED,
                "updated_at": now,
                "revision": state.revision + 1,
            }
        )
        self.store.save(stopped, "session_stopped")
        return stopped

    async def step(self, rest: KalshiRest, pricer: LivePricer, series: str) -> SessionState:
        state = self._state()
        if state.status is not SessionStatus.RUNNING:
            return state
        broker = PaperBroker(state)
        for position in state.positions:
            market = await rest.market(position.ticker)
            if market.result in {"yes", "no"}:
                settled = broker.settle(position.ticker, market.result)
                self.store.save(broker.state, "paper_settled", settled.model_dump(mode="json"))
        state = broker.state
        candidates = await signals(rest, pricer, series)
        if not candidates:
            self.store.save(state, "no_signal", {"series": series})
            return state
        signal = max(candidates, key=lambda item: item.best_edge)
        try:
            position = self._submit(broker, signal)
        except PaperRiskError as exc:
            self.store.save(
                broker.state, "decision_skipped", {"reason": str(exc), "signal": asdict(signal)}
            )
        else:
            self.store.save(broker.state, "paper_filled", position.model_dump(mode="json"))
        return broker.state

    def _state(self) -> SessionState:
        state = self.store.load()
        if state is None:
            raise PaperRiskError("start a paper session first")
        if state.config.mode is not ExecutionMode.PAPER:
            raise PaperRiskError("session is not paper mode")
        return state

    @staticmethod
    def _submit(broker: PaperBroker, signal: Signal) -> PaperPosition:
        side = signal.best_side.upper()
        return broker.submit(
            ticker=signal.ticker,
            side=side,
            yes_price=Decimal(str(signal.ask if side == "YES" else signal.bid)),
            fair_probability=Decimal(str(signal.fair)),
            edge=Decimal(str(signal.best_edge)),
        )


def paper_store(data_dir: Path) -> SessionStore:
    return SessionStore(data_dir / "execution" / "paper")
