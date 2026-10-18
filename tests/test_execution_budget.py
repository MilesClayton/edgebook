"""Regression coverage for owner budget, auto sizing and position management."""

from pathlib import Path
from typing import Any

import pytest

from edgebook.execution.live_ledger import GuardError, ProposalRequest, ReviewRequest
from tests.execution_helpers import TICKER, decision
from tests.test_execution_autopilot import autopilot_service
from tests.test_execution_service import Harness


async def test_owner_limits_are_authoritative(tmp_path: Path) -> None:
    h = Harness(tmp_path / "ledger.sqlite3")
    await h.service.control("resume")
    limits = h.ledger.limits.model_copy(update={"max_contracts": 10, "order_debit_cents": 500})
    await h.service.set_limits(limits)
    review = await h.service.review(ReviewRequest(ticker=TICKER, quantity="4.00"))
    assert review["context"]["limits"]["max_contracts"] == 10
    assert review["context"]["candidates"]["enter_yes"]["intent"]["quantity"] == "4.00"


async def test_budget_sizing_and_fractional_exit(tmp_path: Path) -> None:
    h = Harness(tmp_path / "ledger.sqlite3")
    mode = ["enter_yes"]
    contexts: list[dict[str, Any]] = []

    async def jev(context: dict[str, Any]) -> dict[str, Any]:
        contexts.append(context)
        return decision(mode[0], size="maximum").model_dump()

    service = autopilot_service(h, jev)
    await service.set_budget(2000)
    await service.control("resume")
    await service.set_autopilot(True)
    h.broker.fill_units = 40
    # Weak forecast is evidence for Jev, not an entry veto in explicit budget mode.
    h.fair = 0.42
    note = (await service.autopilot_step())[0]
    assert note["outcome"] == "order canceled"
    assert float(contexts[0]["quantity"]) > 1
    assert h.ledger.status()["positions"][0]["units"] == 40
    mode[0] = "exit"
    # Even if no new markets qualify, existing holdings must still be managed.
    service.tickers = lambda: []
    note = (await service.autopilot_step())[0]
    assert note["order"]["intent"]["action"] == "sell"
    assert note["order"]["intent"]["quantity"] == "0.40"
    assert h.ledger.status()["positions"] == []
    # Cumulative purchases are retained; budget can be reused after closing.
    state = h.ledger.status()
    assert float(state["purchase_spend"]) > 0
    assert float(state["remaining_purchase_budget"]) > 19


async def test_pause_invalidates_auto_proposal(tmp_path: Path) -> None:
    h = Harness(tmp_path / "ledger.sqlite3")

    async def jev(context: dict[str, Any]) -> dict[str, Any]:
        await service.set_autopilot(False)
        return decision().model_dump()

    service = autopilot_service(h, jev)
    await service.set_budget(2000)
    await service.control("resume")
    await service.set_autopilot(True)
    note = (await service.autopilot_step())[0]
    assert "stopped" in note["outcome"]
    assert h.broker.submissions == 0


async def test_budget_reservations_and_losses_never_reset(tmp_path: Path) -> None:
    h = Harness(tmp_path / "ledger.sqlite3")
    await h.service.set_budget(50)
    await h.service.control("resume")
    proposal = await h.proposal()
    await h.service.confirm(h.confirm(proposal))
    # Second entry breaches allowance even though an account holds more cash.
    review = await h.service.review(ReviewRequest(ticker=TICKER + "2", quantity="1.00"))
    second = await h.service.propose(
        ProposalRequest(review_id=review["review_id"], decision=decision())
    )
    with pytest.raises(GuardError, match="capital budget"):
        await h.service.confirm(h.confirm(second, "budget-second-order-0001"))
    spent = h.ledger.status()["purchase_spend"]
    await h.service.set_budget(2000)
    assert h.ledger.status()["paused"] is True
    assert h.ledger.status()["purchase_spend"] == spent


async def test_budget_save_stops_existing_autopilot(tmp_path: Path) -> None:
    h = Harness(tmp_path / "ledger.sqlite3")

    async def jev(context: dict[str, Any]) -> dict[str, Any]:
        return decision().model_dump()

    service = autopilot_service(h, jev)
    await service.control("resume")
    await service.set_autopilot(True)
    await service.set_budget(2000)
    assert h.ledger.status()["paused"] is True
    assert await service.autopilot_step() == []
    assert h.broker.submissions == 0


async def test_cheap_side_can_trade_on_a_small_budget(tmp_path: Path) -> None:
    h = Harness(tmp_path / "ledger.sqlite3")

    async def jev(context: dict[str, Any]) -> dict[str, Any]:
        assert "enter_yes" in context["candidates"]
        assert "enter_no" not in context["candidates"]
        assert context["candidates"]["enter_yes"]["intent"]["quantity"] == "8.00"
        return decision(size="maximum").model_dump()

    service = autopilot_service(h, jev)
    await service.set_budget(50)
    h.broker.yes_bid = 2_000_000
    h.broker.no_bid = 97_000_000
    await service.start("autopilot", h.ledger.status()["generation"])
    note = (await service.autopilot_step())[0]
    assert note["outcome"].startswith("order ")
    assert h.broker.submissions == 1


async def test_start_overtaken_by_stop_is_rejected(tmp_path: Path) -> None:
    h = Harness(tmp_path / "ledger.sqlite3")

    async def jev(context: dict[str, Any]) -> dict[str, Any]:
        return decision().model_dump()

    service = autopilot_service(h, jev)
    await service.set_budget(2000)
    old_generation = h.ledger.status()["generation"]
    await service.control("pause")
    with pytest.raises(GuardError, match="controls changed"):
        await service.start("autopilot", old_generation)
    assert h.ledger.status()["paused"] is True
    assert h.broker.submissions == 0
