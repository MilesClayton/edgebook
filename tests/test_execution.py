from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from edgebook.execution.models import ExecutionMode, SessionConfig, SessionState, SessionStatus
from edgebook.execution.paper import PaperBroker, PaperRiskError
from edgebook.execution.runner import PaperSession
from edgebook.execution.store import SessionStore
from edgebook.live.signals import Signal
from edgebook.venues.kalshi.models import RestMarket


def state() -> SessionState:
    config = SessionConfig(
        mode=ExecutionMode.PAPER,
        paper_starting_cash=Decimal("10"),
        max_open_debit=Decimal("5"),
        max_order_debit=Decimal("1"),
        min_edge=Decimal("0.02"),
    )
    return SessionState(
        session_id="test",
        status=SessionStatus.RUNNING,
        config=config,
        paper_cash=Decimal("10"),
        paper_peak_equity=Decimal("10"),
    )


def test_paper_fill_settlement_and_store(tmp_path: Path) -> None:
    broker = PaperBroker(state())
    position = broker.submit(
        ticker="KX",
        side="YES",
        yes_price=Decimal("0.50"),
        fair_probability=Decimal("0.60"),
        edge=Decimal("0.09"),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert position.debit == Decimal("0.52")  # 50c collateral + rounded 2c taker fee
    assert broker.state.paper_cash == Decimal("9.48")
    settled = broker.settle("KX", "yes", datetime(2026, 1, 1, 1, tzinfo=UTC))
    assert settled.pnl == Decimal("0.48")
    assert broker.state.paper_cash == Decimal("10.48")
    store = SessionStore(tmp_path)
    store.save(broker.state, "settled", {"ticker": "KX"})
    assert store.load() == broker.state
    assert "settled" in store.events_path.read_text()
    history = store.history()
    assert history[-1]["state"]["equity"] == "10.48"
    assert history[-1]["state"]["open_positions"] == 0


def test_paper_risk_caps() -> None:
    broker = PaperBroker(state())
    with pytest.raises(PaperRiskError, match="below"):
        broker.submit(
            ticker="KX",
            side="YES",
            yes_price=Decimal("0.50"),
            fair_probability=Decimal("0.51"),
            edge=Decimal("0.01"),
        )
    with pytest.raises(PaperRiskError, match="per-order"):
        broker.submit(
            ticker="KX",
            side="YES",
            yes_price=Decimal("0.50"),
            fair_probability=Decimal("1"),
            edge=Decimal("0.10"),
            contracts=Decimal("2"),
        )


async def test_paper_session_step_persists_real_quote_simulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path)
    session = PaperSession(store)
    session.start(state().config)
    signal = Signal(
        ticker="KX",
        series="KXBTC15M",
        strike_type="greater_or_equal",
        floor=1.0,
        cap=None,
        close_time=datetime(2026, 1, 1, 1, tzinfo=UTC),
        seconds_left=600,
        spot=1.0,
        fair=0.60,
        bid=0.48,
        ask=0.50,
        edge_yes=0.08,
        edge_no=-0.10,
    )

    async def fake_signals(*_args: object, **_kwargs: object) -> list[Signal]:
        return [signal]

    monkeypatch.setattr("edgebook.execution.runner.signals", fake_signals)

    class Rest:
        async def market(self, _ticker: str) -> RestMarket:
            raise AssertionError("no positions to settle before the first fill")

    stepped = await session.step(Rest(), object(), "KXBTC15M")  # type: ignore[arg-type]
    assert len(stepped.positions) == 1
    assert store.load() == stepped
    assert "paper_filled" in store.events_path.read_text()
