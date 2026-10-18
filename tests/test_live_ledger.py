from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from edgebook.execution.live_ledger import (
    CENT,
    DOLLAR,
    ConfirmRequest,
    GuardError,
    debit,
    fee_reserve,
    money,
    quantity,
)
from edgebook.execution.sqlite_ledger import SQLiteLedger
from tests.execution_helpers import LIMITS, NOW, TICKER, confirmation, fill, intent, ready


def test_exact_fixed_point_and_strict_schema() -> None:
    assert money("0.00000001") == 1
    assert quantity("0.01") == 1
    assert fee_reserve(100) == money("0.0276")
    assert debit(intent()) == money("0.4276")
    for value in ("NaN", "Infinity", "0.000000001"):
        with pytest.raises(GuardError, match="precision"):
            money(value)
    with pytest.raises(GuardError, match="quantity"):
        quantity("1.001")
    for confirmation_value in (True, 1, "true"):
        data = {
            "proposal_id": "a" * 48,
            "confirmation_token": "b" * 43,
            "idempotency_key": "key-idempotency-0001",
            "intent": intent().model_dump(),
            "confirmed": confirmation_value,
        }
        if confirmation_value is True:
            ConfirmRequest.model_validate(data)
        else:
            with pytest.raises(ValidationError):
                ConfirmRequest.model_validate(data)
    with pytest.raises(ValidationError):
        intent(limit_price="0.4")
    with pytest.raises(ValidationError):
        intent(quantity=1)


def test_duplicate_binding_expiry_and_pause(tmp_path: Path) -> None:
    ledger = ready(tmp_path / "ledger.sqlite3")
    request = confirmation(ledger)
    first, fresh = ledger.reserve(request, NOW, 5000)
    assert fresh
    assert ledger.reserve(request, NOW + 999_999, 5000) == (first, False)
    assert len(ledger.orders()) == 1
    with pytest.raises(GuardError, match="changed"):
        ledger.reserve(request.model_copy(update={"intent": intent(quantity="2.00")}), NOW, 5000)
    with pytest.raises(GuardError, match="confirmation"):
        ledger.reserve(request.model_copy(update={"confirmation_token": "z" * 43}), NOW, 5000)
    ledger.mark(request.idempotency_key, "rejected", NOW)
    ledger.account(NOW, DOLLAR, {})
    other = confirmation(ledger)
    with pytest.raises(GuardError, match="different proposal"):
        ledger.reserve(other, NOW, 5000)
    with pytest.raises(GuardError, match="expired"):
        ledger.reserve(
            other.model_copy(update={"idempotency_key": "different-key-0001"}), NOW + 15000, 5000
        )
    ledger.pause(NOW)
    with pytest.raises(GuardError, match="invalidated"):
        ledger.reserve(
            other.model_copy(update={"idempotency_key": "different-key-0001"}), NOW, 5000
        )
    paused = confirmation(ledger, key="paused-request-0001")
    with pytest.raises(GuardError, match="paused"):
        ledger.reserve(paused, NOW, 5000)


def test_independent_connections_serialize_concurrent_reservations(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    primary = ready(path)
    requests = [confirmation(primary, key=f"concurrent-key-{index:04}") for index in range(12)]
    ledgers = [SQLiteLedger(path, LIMITS) for _ in requests]

    def reserve_one(index: int) -> bool:
        try:
            return ledgers[index].reserve(requests[index], NOW, 5000)[1]
        except GuardError:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(reserve_one, range(12))) == 1
    assert len(primary.orders()) == 1
    assert primary.status()["purchase_spend"] == "0.42760000"
    assert primary.status()["unknown_or_pending"]


def test_same_request_concurrent_idempotency(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    primary = ready(path)
    request = confirmation(primary)
    connections = [SQLiteLedger(path, LIMITS) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda ledger: ledger.reserve(request, NOW, 5000), connections))
    assert sum(fresh for _, fresh in results) == 1
    assert len({order["proposal_id"] for order, _ in results}) == 1


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"order_debit_cents": 41}, "per-order"),
        ({"purchase_spend_cents": 41}, "purchase-spend"),
        ({"open_exposure_cents": 41}, "exposure"),
        ({"realized_loss_cents": 41}, "loss"),
    ],
)
def test_fee_inclusive_caps(tmp_path: Path, updates: dict[str, Any], reason: str) -> None:
    limits = LIMITS.model_copy(update=updates)
    ledger = ready(tmp_path / "ledger.sqlite3", limits)
    request = confirmation(ledger)
    with pytest.raises(GuardError, match=reason):
        ledger.reserve(request, NOW, 5000)
    assert not ledger.orders()


def test_partial_fills_restart_cumulative_spend_and_settlement(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = ready(path)
    request = confirmation(ledger, intent(quantity="2.00"))
    ledger.reserve(request, NOW, 5000)
    ledger.mark(request.idempotency_key, "accepted", NOW, "order-1")
    partial = fill(units=50, fee="0.005")
    ledger.reconcile(request.idempotency_key, "order-1", "resting", [partial], NOW)
    assert ledger.status()["purchase_spend"] == "0.85520000"
    assert ledger.status()["positions"][0]["units"] == 50
    restarted = SQLiteLedger(path, LIMITS)
    restarted.recover(NOW + 1)
    assert restarted.orders()[0]["status"] == "unknown"
    assert restarted.status()["paused"]
    assert restarted.status()["purchase_spend"] == "0.85520000"
    restarted.reconcile(request.idempotency_key, "order-1", "canceled", [partial], NOW + 2)
    restarted.reconcile(request.idempotency_key, "order-1", "canceled", [partial], NOW + 3)
    assert restarted.status()["purchase_spend"] == "0.20500000"
    assert restarted.status()["open_exposure"] == "0.20500000"
    restarted.settle(TICKER, 0, NOW + 4)
    restarted.settle(TICKER, 0, NOW + 5)
    status = restarted.status()
    assert status["purchase_spend"] == "0.20500000"
    assert status["open_exposure"] == "0.00000000"
    assert status["realized_loss"] == "0.20500000"
    with pytest.raises(GuardError, match="changed"):
        restarted.settle(TICKER, DOLLAR, NOW + 6)
    # stored caps win over settings (they are owner-edited through set_limits)
    reopened = SQLiteLedger(path, LIMITS.model_copy(update={"purchase_spend_cents": 501}))
    assert reopened.limits == LIMITS


def test_unknown_rejection_and_account_gates(tmp_path: Path) -> None:
    ledger = ready(tmp_path / "ledger.sqlite3")
    request = confirmation(ledger)
    with pytest.raises(GuardError, match="stale"):
        ledger.reserve(request, NOW + 6000, 5000)
    ledger.account(NOW, 1, {})
    with pytest.raises(GuardError, match="balance"):
        ledger.reserve(request, NOW, 5000)
    ledger.account(NOW, DOLLAR, {})
    ledger.reserve(request, NOW, 5000)
    ledger.mark(request.idempotency_key, "unknown", NOW)
    assert ledger.status()["purchase_spend"] == "0.42760000"
    with pytest.raises(GuardError, match="pending"):
        ledger.account(NOW, DOLLAR, {})
    with pytest.raises(GuardError, match="orders"):
        ledger.settle(TICKER, 0, NOW)
    ledger.mark(request.idempotency_key, "rejected", NOW)
    assert ledger.status()["purchase_spend"] == "0.00000000"
    ledger.account(NOW, DOLLAR, {})
    with pytest.raises(GuardError, match="differ"):
        ledger.account(NOW, DOLLAR, {TICKER: 100})


def test_sales_realize_cost_and_do_not_refund_purchase_cap(tmp_path: Path) -> None:
    ledger = ready(tmp_path / "ledger.sqlite3")
    request = confirmation(ledger)
    ledger.reserve(request, NOW, 5000)
    ledger.reconcile(request.idempotency_key, "buy-order", "executed", [fill()], NOW)
    ledger.account(NOW, DOLLAR, {TICKER: 100})
    sale = confirmation(
        ledger, intent(action="sell", limit_price="0.3500"), key="sell-order-key-0001"
    )
    ledger.reserve(sale, NOW, 5000)
    ledger.reconcile(
        sale.idempotency_key,
        "sell-order",
        "canceled",
        [fill("sale-fill", units=50, price="0.3500", fee="0.005")],
        NOW,
    )
    state = ledger.status()
    assert state["positions"][0]["cost"] == money("0.205")
    assert state["realized_loss"] == "0.03500000"
    assert state["purchase_spend"] == "0.41000000"
    ledger.settle(TICKER, DOLLAR, NOW)
    assert ledger.status()["realized_loss"] == "0.03500000"
    assert ledger.status()["realized_pnl"] == "0.26000000"


@pytest.mark.parametrize(
    ("selected", "reason"),
    [
        (intent(quantity="0.00"), "quantity"),
        (intent(quantity="4.00"), "quantity"),
        (intent(limit_price="0.0000"), "price"),
        (intent(action="sell"), "position"),
    ],
)
def test_crafted_order_validation(tmp_path: Path, selected: Any, reason: str) -> None:
    ledger = ready(tmp_path / "ledger.sqlite3")
    with pytest.raises(GuardError, match=reason):
        ledger.reserve(confirmation(ledger, selected), NOW, 5000)


def test_reconciliation_rejects_bad_fills_transactionally(tmp_path: Path) -> None:
    ledger = ready(tmp_path / "ledger.sqlite3")
    request = confirmation(ledger)
    ledger.reserve(request, NOW, 5000)
    for fills in (
        [fill(units=101)],
        [fill(price="0.4100")],
        [fill(fee="-0.01")],
        [fill(fee="0.05")],
        [fill(units=-1)],
    ):
        with pytest.raises(GuardError):
            ledger.reconcile(request.idempotency_key, "order", "executed", fills, NOW)
    assert ledger.status()["positions"] == []
    ledger.reconcile(request.idempotency_key, "order", "canceled", [fill()], NOW)
    with pytest.raises(GuardError, match="changed"):
        ledger.reconcile(request.idempotency_key, "order", "canceled", [fill(fee="0.011")], NOW)
    assert ledger.status()["purchase_spend"] == "0.41000000"
    with pytest.raises(GuardError, match="quantity"):
        ledger.reconcile(request.idempotency_key, "order", "canceled", [], NOW)
    with pytest.raises(GuardError, match="status"):
        ledger.mark(request.idempotency_key, "done", NOW)
    with pytest.raises(GuardError, match="pending"):
        ledger.mark(request.idempotency_key, "rejected", NOW)
    with pytest.raises(GuardError, match="broker"):
        ledger.reconcile(request.idempotency_key, "other", "canceled", [fill()], NOW)
    with pytest.raises(GuardError, match="status"):
        ledger.reconcile(request.idempotency_key, "order", "invalid", [], NOW)
    with pytest.raises(GuardError, match="settlement"):
        ledger.settle(TICKER, -1, NOW)
    assert CENT == DOLLAR // 100


def test_expired_data_after_wait_and_unknown_legacy_database(tmp_path: Path) -> None:
    ledger = ready(tmp_path / "ledger.sqlite3")
    request = confirmation(ledger)
    with pytest.raises(GuardError, match="data expired"):
        ledger.reserve(
            request, NOW, 5000, validation_expires_ms=NOW + 1000, clock=lambda: NOW + 1000
        )
    assert ledger.orders() == []
    legacy = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE intents (key TEXT)")
    with pytest.raises(GuardError, match="legacy"):
        SQLiteLedger(legacy, LIMITS)


def test_closed_winner_does_not_restore_cumulative_purchase_budget(tmp_path: Path) -> None:
    ledger = ready(
        tmp_path / "ledger.sqlite3",
        LIMITS.model_copy(update={"purchase_spend_cents": 75}),
    )
    first = confirmation(ledger)
    ledger.reserve(first, NOW, 5000)
    ledger.reconcile(first.idempotency_key, "winning-order", "executed", [fill()], NOW)
    ledger.settle(TICKER, DOLLAR, NOW)
    ledger.account(NOW, 10 * DOLLAR, {})
    second = confirmation(ledger, key="new-purchase-key-0001")
    with pytest.raises(GuardError, match="purchase-spend"):
        ledger.reserve(second, NOW, 5000)
    assert ledger.status()["open_exposure"] == "0.00000000"
    assert ledger.status()["purchase_spend"] == "0.41000000"
