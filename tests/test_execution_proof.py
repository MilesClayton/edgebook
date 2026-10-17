"""Real-money entries require a paper-proven strategy; exits never do."""

from pathlib import Path

import pytest

from edgebook.execution.live_ledger import GuardError, LiveSettings, ReviewRequest
from edgebook.execution.proof import paper_proof
from tests.test_execution_service import Harness

DAY_MS = 86_400_000
T0 = 1_792_108_800_000  # 2026-10-16 00:00 UTC


def record(n: int, days: int, pnl: float) -> list[tuple[float, float, int]]:
    return [(pnl + (0.01 if i % 2 else -0.01), 1.0, T0 + (i % days) * DAY_MS + i) for i in range(n)]


def test_paper_proof_needs_trades_days_and_profit_beyond_luck() -> None:
    few = paper_proof(record(50, 6, 0.05), 200, 5)
    assert not few["proven"]
    assert few["reason"] == "50 closed paper trades; 200 required"
    one_day = paper_proof(record(300, 1, 0.05), 200, 5)
    assert not one_day["proven"]
    assert "span 1 days" in one_day["reason"]
    losing = paper_proof(record(300, 6, -0.02), 200, 5)
    assert not losing["proven"]
    assert "does not exclude zero" in losing["reason"]
    good = paper_proof(record(300, 6, 0.02), 200, 5)
    assert good["proven"]
    assert good["ci_lo"] > 0
    assert (good["trades"], good["days"]) == (300, 6)
    empty = paper_proof([], 200, 5)
    assert not empty["proven"]
    assert empty["mean_per_contract"] is None
    undated = paper_proof([(0.05, 1.0, 0)] * 300, 200, 5)  # times unknown: no days counted
    assert undated["days"] == 0


def test_hourly_strike_tickers_are_accepted() -> None:
    # hourly tickers contain "." (strike 81999.99); the original pattern rejected all of them
    assert ReviewRequest(ticker="KXBTCD-26OCT1717-T81999.99", quantity="1.00").ticker.endswith(
        ".99"
    )
    with pytest.raises(ValueError, match="pattern"):
        ReviewRequest(ticker="kxbtcd lower", quantity="1.00")


def test_gate_floors_cannot_be_configured_away() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 100"):
        LiveSettings(proof_min_trades=10)
    with pytest.raises(ValueError, match="greater than or equal to 3"):
        LiveSettings(proof_min_days=1)
    assert LiveSettings().proof_strategy == "anchored"


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "ledger.sqlite3")


async def test_unproven_strategy_blocks_entries_but_not_exits(harness: Harness) -> None:
    await harness.service.control("resume")
    entry = await harness.proposal()  # proposed while the paper record was proven
    harness.proven = {"proven": False, "reason": "12 closed paper trades; 200 required"}
    with pytest.raises(GuardError, match="blocked until the 'anchored' paper strategy"):
        await harness.service.confirm(harness.confirm(entry))  # re-checked at confirmation
    assert harness.broker.submissions == 0
    with pytest.raises(GuardError, match="200 required"):
        await harness.proposal()
    status = await harness.service.status()
    assert status["paper_proof"] == {
        "strategy": "anchored",
        "proven": False,
        "reason": "12 closed paper trades; 200 required",
    }
    harness.proven = {"proven": True, "reason": "fixture"}
    bought = await harness.service.confirm(
        harness.confirm(await harness.proposal(), "proof-key-00000001")
    )
    assert bought["status"] == "executed"
    harness.proven = {"proven": False, "reason": "lost its edge"}
    harness.fair = 0.1
    sale = await harness.proposal("exit")  # exits reduce risk: always allowed
    exited = await harness.service.confirm(harness.confirm(sale, "proof-sell-0000001"))
    assert exited["intent"]["action"] == "sell"
    with harness.ledger.connection() as db:
        kinds = [row[0] for row in db.execute("SELECT kind FROM audit")]
    assert kinds.count("entry_blocked_unproven") == 2
