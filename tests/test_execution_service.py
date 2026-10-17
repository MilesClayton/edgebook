from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from edgebook.execution.broker import BrokerRejectedError
from edgebook.execution.live_ledger import (
    DOLLAR,
    ConfirmRequest,
    GuardError,
    Intent,
    ProposalRequest,
    ReviewRequest,
    money,
)
from edgebook.execution.service import ExecutionService
from edgebook.execution.sqlite_ledger import SQLiteLedger
from edgebook.recorder.http import make_app
from tests.execution_helpers import LIMITS, NOW, TICKER, decision, fill, live_settings


class FixtureBroker:
    def __init__(self, clock: list[int]) -> None:
        self.clock = clock
        self.positions: dict[str, int] = {}
        self.balance = 100 * DOLLAR
        self.submissions = 0
        self.failure = ""
        self.fill_units = 100
        self.orders: dict[str, dict[str, Any]] = {}
        self.fills: dict[str, list[dict[str, Any]]] = {}
        self.yes_bid = money("0.39")
        self.no_bid = money("0.60")
        self.age = 0
        self.depth = 10000
        self.market_status = "active"
        self.quote_age = 0
        self.close_offset = 300

    async def account(self) -> dict[str, Any]:
        if self.failure == "account":
            raise OSError("offline fixture")
        return {
            "as_of_ms": self.clock[0] - self.age,
            "balance": self.balance,
            "positions": dict(self.positions),
        }

    async def market(self, ticker: str) -> dict[str, Any]:
        return {
            "ticker": ticker,
            "event_ticker": "KXBTC15M-26OCT17",
            "market_type": "binary",
            "notional_value_dollars": "1.0000",
            "fractional_trading_enabled": True,
            "status": self.market_status,
            "close_time": datetime.fromtimestamp(NOW / 1000 + self.close_offset, UTC).isoformat(),
            "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0001"}],
            "settlement_value_dollars": "1.0000",
        }

    async def quote(self, ticker: str, series: str) -> dict[str, Any]:
        return {
            "market": await self.market(ticker),
            "observed_ms": self.clock[0] - self.quote_age,
            "bids": {"yes": (self.yes_bid, self.depth), "no": (self.no_bid, self.depth)},
        }

    async def submit(
        self, selected: Intent, client_id: str, deadline_ms: int | None = None
    ) -> dict[str, Any]:
        self.submissions += 1
        if self.failure == "reject":
            raise BrokerRejectedError("fixture rejected")
        if self.failure == "timeout_before":
            raise TimeoutError("fixture timeout")
        if self.failure == "cancel":
            raise asyncio.CancelledError()
        identifier = f"fixture-order-{self.submissions}"
        units = min(self.fill_units, selected.units)
        self.orders[client_id] = {
            "order_id": identifier,
            "client_order_id": client_id,
            "status": "executed" if units == selected.units else "canceled",
        }
        self.fills[identifier] = (
            [] if not units else [fill(identifier + "-fill", units, selected.limit_price, "0.005")]
        )
        direction = (1 if selected.side == "yes" else -1) * (1 if selected.action == "buy" else -1)
        self.positions[selected.ticker] = self.positions.get(selected.ticker, 0) + direction * units
        if self.failure == "timeout_after":
            raise TimeoutError("fixture timeout after accept")
        return {"order_id": identifier, "client_order_id": client_id, "ts_ms": self.clock[0]}

    async def find_order(self, client_id: str, order_id: str | None) -> dict[str, Any] | None:
        if self.failure == "read":
            raise OSError("fixture broker read unavailable")
        return self.orders.get(client_id)

    async def order_fills(self, order: dict[str, Any], selected: Intent) -> list[dict[str, Any]]:
        return self.fills[order["order_id"]]


class Harness:
    def __init__(self, path: Path) -> None:
        self.clock = [NOW]
        self.broker = FixtureBroker(self.clock)
        self.fair = 0.70
        self.index_age: int | None = 0
        self.ws_age: int | None = 0
        self.gates: dict[str, Any] = {"warmup": True, "index": True}
        self.series = "KXBTC15M"
        self.engine_quote_mismatch = False
        self.settings = live_settings()
        self.ledger = SQLiteLedger(path, LIMITS)
        self.proven: dict[str, Any] = {"proven": True, "reason": "fixture"}
        self.service = ExecutionService(
            self.settings,
            self.ledger,
            self.broker,
            self.source,
            lambda: self.clock[0],
            proof=lambda: self.proven,
        )

    def source(self, ticker: str, position: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "updated_ms": self.clock[0],
            "ws_age_ms": self.ws_age,
            "call": {
                "ticker": ticker,
                "series": self.series,
                "fair_up": self.fair,
                "model_up": self.fair,
                "index_age_ms": self.index_age,
                "gates": self.gates,
                "up_price": (DOLLAR - self.broker.no_bid) / DOLLAR
                + (0.01 if self.engine_quote_mismatch else 0),
                "down_price": (DOLLAR - self.broker.yes_bid) / DOLLAR,
            },
        }

    async def proposal(self, action: str = "enter_yes", amount: str = "1.00") -> dict[str, Any]:
        review = await self.service.review(ReviewRequest(ticker=TICKER, quantity=amount))
        return await self.service.propose(
            ProposalRequest(
                review_id=review["review_id"],
                decision=decision(action),
            )
        )

    @staticmethod
    def confirm(proposal: dict[str, Any], key: str = "service-key-0000001") -> ConfirmRequest:
        return ConfirmRequest(
            proposal_id=proposal["proposal_id"],
            confirmation_token=proposal["confirmation_token"],
            idempotency_key=key,
            intent=Intent.model_validate(proposal["intent"]),
            confirmed=True,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "ledger.sqlite3")


async def test_end_to_end_confirm_exit_and_audit(harness: Harness) -> None:
    await harness.service.control("resume")
    proposal = await harness.proposal()
    assert not proposal["submitted"]
    assert harness.broker.submissions == 0
    request = harness.confirm(proposal)
    result = await harness.service.confirm(request)
    assert result["status"] == "executed"
    assert result["broker_acknowledged"]
    assert result["submitted"]
    assert (await harness.service.confirm(request)) == result
    assert harness.broker.submissions == 1
    harness.fair = 0.1
    sale = await harness.proposal("exit")
    exited = await harness.service.confirm(harness.confirm(sale, "service-sell-000001"))
    assert exited["intent"]["action"] == "sell"
    assert exited["status"] == "executed"
    status = await harness.service.status()
    assert status["positions"] == []
    assert status["purchase_spend"] == "0.40500000"
    assert status["unattended_enabled"] is False
    with harness.ledger.connection() as db:
        events = {row[0] for row in db.execute("SELECT kind FROM audit")}
    assert {
        "review",
        "model_decision",
        "proposal",
        "confirmation_checks",
        "reservation",
        "fill",
        "broker_receipt",
        "account",
        "reconciled",
    }.issubset(events)


@pytest.mark.parametrize("failure", ["reject", "timeout_before", "timeout_after", "read"])
async def test_rejection_unknown_and_restart(harness: Harness, failure: str) -> None:
    await harness.service.control("resume")
    proposal = await harness.proposal()
    harness.broker.failure = failure
    request = harness.confirm(proposal)
    result = await harness.service.confirm(request)
    if failure == "reject":
        assert result["status"] == "rejected"
        assert harness.ledger.status()["purchase_spend"] == "0.00000000"
    else:
        assert result["status"] == "unknown"
        assert harness.ledger.status()["purchase_spend"] == "0.42760000"
        assert harness.ledger.status()["paused"]
        harness.ledger.recover(NOW + 1)
        harness.broker.failure = ""
        if failure == "timeout_before":
            with pytest.raises(GuardError, match="absent"):
                await harness.service.control("reconcile")
        else:
            await harness.service.control("reconcile")
            assert harness.ledger.orders()[0]["status"] == "executed"
            assert harness.ledger.status()["purchase_spend"] == "0.40500000"
    assert harness.broker.submissions == 1
    await harness.service.confirm(request)
    assert harness.broker.submissions == 1


async def test_partial_fill_and_authoritative_settlement(harness: Harness) -> None:
    await harness.service.control("resume")
    harness.broker.fill_units = 37
    result = await harness.service.confirm(harness.confirm(await harness.proposal()))
    assert result["filled_quantity"] == "0.37"
    assert result["status"] == "canceled"
    assert harness.ledger.status()["positions"][0]["units"] == 37
    harness.broker.market_status = "settled"
    harness.broker.positions = {}
    await harness.service.control("reconcile")
    status = harness.ledger.status()
    assert not status["positions"]
    assert status["realized_pnl"] == "0.21700000"
    assert status["purchase_spend"] == "0.15300000"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("index_age", 3001, "stale"),
        ("index_age", None, "invalid"),
        ("ws_age", -1, "stale"),
        ("ws_age", None, "invalid"),
        ("gates", {}, "gates"),
        ("gates", {"forged": 1}, "gates"),
        ("fair", float("nan"), "probability"),
        ("fair", 1.1, "probability"),
        ("series", "OTHER", "series"),
        ("engine_quote_mismatch", True, "book"),
    ],
)
async def test_market_gates(harness: Harness, field: str, value: Any, reason: str) -> None:
    setattr(harness, field, value)
    with pytest.raises(GuardError, match=reason):
        await harness.proposal()
    assert harness.broker.submissions == 0


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("age", 5001, "stale"),
        ("market_status", "closed", "closed"),
        ("quote_age", 3001, "quote"),
        ("close_offset", 4, "closed"),
    ],
)
async def test_account_quote_and_timing_gates(
    harness: Harness, field: str, value: Any, reason: str
) -> None:
    setattr(harness.broker, field, value)
    with pytest.raises(GuardError, match=reason):
        await harness.proposal()
    assert harness.broker.submissions == 0


async def test_skip_hold_and_unavailable_candidates(harness: Harness) -> None:
    for action in ("skip", "hold"):
        result = await harness.proposal(action)
        assert result["status"] == action
        assert not result["submitted"]
    for action, change in (
        ("enter_yes", ("close_offset", 59)),
        ("enter_yes", ("depth", 1)),
    ):
        setattr(harness.broker, *change)
        with pytest.raises(GuardError, match="deterministic"):
            await harness.proposal(action)
        harness.broker.close_offset, harness.broker.depth = 300, 10000
    harness.fair = 0.42
    with pytest.raises(GuardError, match="deterministic"):
        await harness.proposal()


async def test_no_model_or_crafted_client_bypass(harness: Harness) -> None:
    review = await harness.service.review(ReviewRequest(ticker=TICKER, quantity="1.00"))
    for selected in (
        decision(mode="fixture"),
        decision(model="other"),
        decision(confidence=0.69),
        decision(action="enter_no"),
    ):
        with pytest.raises(GuardError):
            await harness.service.propose(
                ProposalRequest(review_id=review["review_id"], decision=selected)
            )
    await harness.service.control("resume")
    proposal = await harness.proposal()
    request = harness.confirm(proposal)
    with pytest.raises(GuardError, match="changed"):
        await harness.service.confirm(
            request.model_copy(
                update={"intent": request.intent.model_copy(update={"quantity": "2.00"})}
            )
        )
    harness.broker.no_bid = money("0.59")
    with pytest.raises(GuardError, match="quote"):
        await harness.service.confirm(request)
    assert harness.broker.submissions == 0


async def test_expiry_pause_stale_account_and_disabled(harness: Harness) -> None:
    await harness.service.control("resume")
    proposal = await harness.proposal()
    harness.clock[0] += 15000
    with pytest.raises(GuardError, match="expired"):
        await harness.service.confirm(harness.confirm(proposal))
    proposal = await harness.proposal()
    await harness.service.control("pause")
    with pytest.raises(GuardError, match="invalidated"):
        await harness.service.confirm(harness.confirm(proposal))
    harness.broker.positions = {TICKER: 1}
    with pytest.raises(GuardError, match="differ"):
        await harness.service.control("resume")
    assert (await harness.service.status())["paused"]
    harness.service.settings = harness.settings.model_copy(update={"enabled": False})
    with pytest.raises(GuardError, match="disabled"):
        await harness.proposal()
    with pytest.raises(GuardError, match="unknown"):
        await harness.service.control("unattended")
    assert harness.broker.submissions == 0


async def test_http_auth_roles_strict_fields_and_confirmation(
    harness: Harness, tmp_path: Path
) -> None:
    settings = harness.settings
    app = make_app(
        tmp_path, "dashboard-only", harness.service, settings.api_token, settings.proposal_token
    )
    operator = {"Authorization": "Bearer " + settings.api_token}
    proposer = {"Authorization": "Bearer " + settings.proposal_token}
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/execution/status")).status == 401
        assert (await client.get("/execution/status", headers=proposer)).status == 401
        assert (await client.post("/execution/review", json={}, headers=operator)).status == 401
        assert (
            await client.post("/execution/resume", json={"confirmed": 1}, headers=operator)
        ).status == 409
        assert (
            await client.post("/execution/resume", json={"confirmed": True}, headers=operator)
        ).status == 200
        assert (
            await client.post(
                "/execution/review",
                json={
                    "ticker": TICKER,
                    "quantity": "1.00",
                    "account_fresh": True,
                },
                headers=proposer,
            )
        ).status == 400
        assert (await client.post("/execution/review", data="{}", headers=proposer)).status == 409
        response = await client.post(
            "/execution/review",
            json={
                "ticker": TICKER,
                "quantity": "1.00",
            },
            headers=proposer,
        )
        review = await response.json()
        proposed = await (
            await client.post(
                "/execution/propose",
                json={
                    "review_id": review["review_id"],
                    "decision": decision().model_dump(),
                },
                headers=proposer,
            )
        ).json()
        request = harness.confirm(proposed).model_dump()
        assert (
            await client.post("/execution/confirm", json=request, headers=proposer)
        ).status == 401
        response = await client.post("/execution/confirm", json=request, headers=operator)
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert (await response.json())["status"] == "executed"
        assert (await client.get("/execution/confirm", headers=operator)).status == 404
        assert (await client.get("/execution/status", headers=operator)).status == 200
    disabled = make_app(tmp_path, "dashboard-only", operator_token=settings.api_token)
    async with TestClient(TestServer(disabled)) as client:
        body = await (await client.get("/execution/status", headers=operator)).json()
        assert not body["configured"]
        assert body["paused"]
        assert (
            await client.post("/execution/confirm", json=request, headers=operator)
        ).status == 503


async def test_cancellation_retains_unknown_reservation(harness: Harness) -> None:
    await harness.service.control("resume")
    proposal = await harness.proposal()
    harness.broker.failure = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await harness.service.confirm(harness.confirm(proposal))
    assert harness.ledger.orders()[0]["status"] == "unknown"
    assert harness.ledger.status()["purchase_spend"] == "0.42760000"


async def test_skip_outcome_is_logged_without_a_trade(harness: Harness) -> None:
    await harness.proposal("skip")
    assert harness.broker.submissions == 0
    harness.clock[0] += 301000
    harness.broker.market_status = "settled"
    await harness.service.control("reconcile")
    assert harness.ledger.due_outcomes(harness.clock[0]) == []
    with harness.ledger.connection() as db:
        outcome = db.execute("SELECT body FROM audit WHERE kind='market_outcome'").fetchone()
    assert outcome is not None
    assert TICKER in outcome[0]
