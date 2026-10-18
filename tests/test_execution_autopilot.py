from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from edgebook.execution.live_ledger import GuardError
from edgebook.execution.service import ExecutionService
from edgebook.execution.sqlite_ledger import SQLiteLedger
from edgebook.recorder.http import make_app
from tests.execution_helpers import LIMITS, TICKER, decision
from tests.test_execution_service import Harness


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "ledger.sqlite3")


def autopilot_service(harness: Harness, decide: Any) -> ExecutionService:
    harness.service = ExecutionService(
        harness.settings,
        harness.ledger,
        harness.broker,
        harness.source,
        lambda: harness.clock[0],
        proof=lambda: harness.proven,
        decide=decide,
        tickers=lambda: [TICKER],
    )
    return harness.service


async def test_autopilot_trades_within_caps_and_never_adds(harness: Harness) -> None:
    asked: list[dict[str, Any]] = []

    async def jev(context: dict[str, Any]) -> dict[str, Any]:
        asked.append(context)
        return decision("enter_yes").model_dump()

    service = autopilot_service(harness, jev)
    assert await service.autopilot_step() == []  # off by default
    await service.set_autopilot(True)
    assert await service.autopilot_step() == []  # still paused
    await service.control("resume")
    notes = await service.autopilot_step()
    assert notes[0]["outcome"] == "order executed"
    assert notes[0]["jev"] == {"action": "enter_yes", "confidence": 0.9}
    assert harness.broker.submissions == 1
    status = await service.status()
    assert status["autopilot"] is True
    assert status["autopilot_last"] == notes
    # holding now: it never buys more of the same market
    await service.autopilot_step()
    assert harness.broker.submissions == 1
    await service.set_autopilot(False)
    assert await service.autopilot_step() == []
    with harness.ledger.connection() as db:
        kinds = [row[0] for row in db.execute("SELECT kind FROM audit")]
    assert "autopilot" in kinds
    assert "autopilot_order" in kinds


async def test_autopilot_skips_without_edge_and_survives_errors(harness: Harness) -> None:
    calls = 0

    async def jev(context: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("gateway down")
        return decision("skip").model_dump()

    service = autopilot_service(harness, jev)
    await service.control("resume")
    await service.set_autopilot(True)
    harness.fair = 0.42
    assert (await service.autopilot_step())[0]["outcome"].startswith("no trade")
    assert calls == 0  # no edge: Jev is not asked
    harness.fair = 0.70
    assert (await service.autopilot_step())[0]["outcome"] == "Jev chose skip: no order"
    assert (await service.autopilot_step())[0]["outcome"] == "error: RuntimeError"
    note = (await service.autopilot_step())[0]
    assert note["engine"]["fair_up"] == 0.70
    assert note["engine"]["edge_needed"] == pytest.approx(
        harness.settings.min_edge + harness.settings.latency_buffer
    )
    harness.gates = {"warmup": False, "index": True}
    note = (await service.autopilot_step())[0]
    assert note["outcome"] == "refused: deterministic data-quality gates failed: warmup"
    assert note["engine"]["failed_gates"] == ["warmup"]  # the popup names what it waits for
    harness.gates = {"warmup": True, "index": True}
    harness.index_age = 99999
    assert (await service.autopilot_step())[0]["outcome"].startswith("refused: ")
    assert harness.broker.submissions == 0
    assert (await service.status())["autopilot_every_s"] == harness.settings.autopilot_every_s


async def test_autopilot_requires_decision_url(harness: Harness) -> None:
    with pytest.raises(GuardError, match="not configured"):
        await harness.service.set_autopilot(True)
    assert (await harness.service.status())["autopilot_available"] is False
    await harness.service.set_autopilot(False)


async def test_owner_caps_change_is_kept_and_audited(harness: Harness, tmp_path: Path) -> None:
    raised = LIMITS.model_copy(update={"purchase_spend_cents": 5000, "realized_loss_cents": 1500})
    await harness.service.set_limits(raised)
    assert harness.ledger.status()["limits"]["purchase_spend_cents"] == 5000
    # a restart with the originally configured caps keeps the owner's caps
    reopened = SQLiteLedger(tmp_path / "ledger.sqlite3", LIMITS)
    assert reopened.limits.realized_loss_cents == 1500
    with harness.ledger.connection() as db:
        changed = db.execute("SELECT COUNT(*) FROM audit WHERE kind='limits_changed'").fetchone()
    assert changed[0] == 1


async def test_http_limits_and_autopilot_need_operator_confirmation(
    harness: Harness, tmp_path: Path
) -> None:
    settings = harness.settings
    app = make_app(
        tmp_path, "dashboard-only", harness.service, settings.api_token, settings.proposal_token
    )
    operator = {"Authorization": "Bearer " + settings.api_token}
    proposer = {"Authorization": "Bearer " + settings.proposal_token}
    caps = {**LIMITS.model_dump(), "purchase_spend_cents": 5000}
    body = {"confirmed": True, "limits": caps}
    async with TestClient(TestServer(app)) as client:

        async def post(action: str, payload: Any, headers: dict[str, str] = operator) -> int:
            return (await client.post(f"/execution/{action}", json=payload, headers=headers)).status

        assert await post("limits", body, proposer) == 401
        assert await post("limits", {"limits": caps}) == 409
        assert (
            await post("limits", {"confirmed": True, "limits": {**caps, "max_contracts": 0}}) >= 400
        )
        response = await client.post("/execution/limits", json=body, headers=operator)
        assert response.status == 200
        assert (await response.json())["limits"]["purchase_spend_cents"] == 5000
        assert await post("autopilot", {"confirmed": True, "enabled": "yes"}) == 409
        assert await post("autopilot", {"confirmed": True, "enabled": True}) == 409  # no Jev URL
        assert await post("autopilot", {"confirmed": True, "enabled": False}) == 200
