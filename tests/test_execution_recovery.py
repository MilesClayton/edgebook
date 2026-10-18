"""Transient broker interruption recovers; owner's Stop and ambiguity never do."""

from pathlib import Path

import pytest

from edgebook.execution.live_ledger import GuardError
from tests.execution_helpers import TICKER
from tests.test_execution_service import Harness


async def running(path: Path) -> Harness:
    h = Harness(path)
    await h.service.control("resume")
    h.ledger.set_autopilot(True, h.clock[0])
    return h


async def clean_check(h: Harness) -> bool:
    h.clock[0] += 1000
    await h.service.refresh()
    return h.ledger.recover_if_ready(h.clock[0], h.settings.account_max_age_ms)


async def test_three_clean_checks_resume_only_a_temporary_stop(tmp_path: Path) -> None:
    h = await running(tmp_path / "ledger.sqlite3")
    h.broker.failure = "account"
    with pytest.raises(OSError, match="offline fixture"):
        await h.service.refresh()
    state = h.ledger.status()
    assert state["paused"]
    assert state["auto_recovery"]
    h.broker.failure = ""
    assert not await clean_check(h)
    # Frequent UI refreshes cannot accelerate recovery.
    assert not h.ledger.recover_if_ready(h.clock[0], h.settings.account_max_age_ms)
    assert h.ledger.status()["recovery_checks"] == 1
    assert not await clean_check(h)
    assert await clean_check(h)
    assert not h.ledger.status()["paused"]
    assert h.broker.submissions == 0


async def test_stop_during_recovery_remains_stopped(tmp_path: Path) -> None:
    h = await running(tmp_path / "ledger.sqlite3")
    h.ledger.pause(h.clock[0], "temporary", recoverable=True)
    assert not await clean_check(h)
    await h.service.control("pause")
    for _ in range(4):
        assert not await clean_check(h)
    assert h.ledger.status()["paused"]
    assert not h.ledger.status()["auto_recovery"]
    assert h.ledger.status()["error"] == "user paused"
    h.broker.failure = "account"
    with pytest.raises(OSError, match="offline fixture"):
        await h.service.refresh()
    assert h.ledger.status()["error"] == "user paused"


async def test_failure_restarts_clean_check_counter(tmp_path: Path) -> None:
    h = await running(tmp_path / "ledger.sqlite3")
    h.ledger.pause(h.clock[0], "temporary", recoverable=True)
    assert not await clean_check(h)
    h.broker.failure = "account"
    with pytest.raises(OSError, match="offline fixture"):
        await h.service.refresh()
    assert h.ledger.status()["recovery_checks"] == 0
    h.broker.failure = ""
    assert not await clean_check(h)
    assert not await clean_check(h)
    assert await clean_check(h)


async def test_account_mismatch_and_restart_are_latched(tmp_path: Path) -> None:
    h = await running(tmp_path / "ledger.sqlite3")
    h.broker.positions[TICKER] = 100
    with pytest.raises(GuardError, match="positions differ"):
        await h.service.refresh()
    assert not h.ledger.status()["auto_recovery"]
    h.broker.positions.clear()
    for _ in range(3):
        assert not await clean_check(h)
    assert h.ledger.status()["paused"]
    await h.service.control("resume")
    h.ledger.pause(h.clock[0], "temporary", recoverable=True)
    h.ledger.recover(h.clock[0])
    assert not h.ledger.status()["auto_recovery"]
    assert h.ledger.status()["pause_kind"] == "restart"


async def test_unknown_order_never_enables_recovery(tmp_path: Path) -> None:
    h = await running(tmp_path / "ledger.sqlite3")
    proposal = await h.proposal()
    h.broker.failure = "timeout_before"
    result = await h.service.confirm(h.confirm(proposal))
    assert result["status"] == "unknown"
    assert h.ledger.status()["unknown_or_pending"]
    assert not h.ledger.status()["auto_recovery"]
