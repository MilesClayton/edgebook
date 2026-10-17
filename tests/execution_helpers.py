from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from edgebook.execution.live_ledger import (
    DOLLAR,
    ConfirmRequest,
    Intent,
    Limits,
    LiveSettings,
    ModelDecision,
    dollars,
    money,
)
from edgebook.execution.sqlite_ledger import SQLiteLedger

NOW = 1_792_250_000_000
TICKER = "KXBTC15M-26OCT17-X"
LIMITS = Limits(
    purchase_spend_cents=500,
    open_exposure_cents=300,
    realized_loss_cents=300,
    order_debit_cents=100,
    max_contracts=3,
)


def intent(**updates: Any) -> Intent:
    return Intent.model_validate(
        {
            "ticker": TICKER,
            "action": "buy",
            "side": "yes",
            "quantity": "1.00",
            "limit_price": "0.4000",
            **updates,
        }
    )


def ready(path: Path, limits: Limits = LIMITS) -> SQLiteLedger:
    ledger = SQLiteLedger(path, limits)
    ledger.account(NOW, 100 * DOLLAR, {})
    ledger.resume(NOW, 5000)
    return ledger


def decision(action: str = "enter_yes", **updates: Any) -> ModelDecision:
    return ModelDecision.model_validate(
        {
            "action": action,
            "confidence": 0.9,
            "mode": "gateway",
            "model": "typesafe-ai/jev",
            "latency_ms": 15,
            **updates,
        }
    )


def confirmation(
    ledger: SQLiteLedger, selected: Intent | None = None, key: str = "idempotency-key-0001"
) -> ConfirmRequest:
    selected = selected or intent()
    proposal = ledger.proposal(
        {
            "intent": selected.model_dump(),
            "decision": decision().model_dump(),
            "context": {"observed_ms": NOW},
        },
        NOW,
        15000,
    )
    return ConfirmRequest(
        proposal_id=proposal["proposal_id"],
        confirmation_token=proposal["confirmation_token"],
        idempotency_key=key,
        intent=selected,
        confirmed=True,
    )


def fill(
    identifier: str = "fill-1", units: int = 100, price: str = "0.4000", fee: str = "0.01"
) -> dict[str, Any]:
    return {
        "id": identifier,
        "units": units,
        "cost": money(price) * units // 100,
        "fee": money(fee),
        "ts": NOW,
        "yes_price": money(price),
        "is_taker": True,
    }


def live_settings(**updates: Any) -> LiveSettings:
    return LiveSettings(
        enabled=True,
        direct_member=True,
        topology="single_host_durable_sqlite",
        api_token="operator-" * 8,
        proposal_token="proposal-" * 8,
        max_contracts=3,
        **updates,
    )


def price_string(price: int) -> str:
    return f"{Decimal(dollars(price)):.4f}"
