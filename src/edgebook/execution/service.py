"""Authoritative review, confirmation, risk, and reconciliation workflow."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from decimal import Decimal
from typing import Any, Literal, Protocol

from edgebook.execution.broker import BrokerRejectedError, timestamp
from edgebook.execution.live_ledger import (
    DOLLAR,
    ConfirmRequest,
    GuardError,
    Intent,
    LiveSettings,
    ProposalRequest,
    ReviewRequest,
    debit,
    dollars,
    fee_reserve,
    money,
    quantity,
)
from edgebook.execution.sqlite_ledger import SQLiteLedger


class Broker(Protocol):
    async def account(self) -> dict[str, Any]: ...
    async def market(self, ticker: str) -> dict[str, Any]: ...
    async def quote(self, ticker: str, series: str) -> dict[str, Any]: ...
    async def submit(
        self, intent: Intent, client_id: str, deadline_ms: int | None = None
    ) -> dict[str, Any]: ...
    async def find_order(self, client_id: str, order_id: str | None) -> dict[str, Any] | None: ...
    async def order_fills(self, order: dict[str, Any], intent: Intent) -> list[dict[str, Any]]: ...


def finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise GuardError(f"missing or invalid {label}")
    return float(value)


class ExecutionService:
    def __init__(
        self,
        settings: LiveSettings,
        ledger: SQLiteLedger,
        broker: Broker,
        source: Callable[[str, dict[str, Any] | None], dict[str, Any]],
        clock: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        *,
        proof: Callable[[], dict[str, Any]],
    ) -> None:
        self.settings, self.ledger, self.broker = settings, ledger, broker
        self.source, self.clock = source, clock
        self.proof = proof  # the paper record of the strategy whose prices execution uses
        self.lock = asyncio.Lock()

    def require_proof(self, route: str) -> None:
        """New entries need a paper-proven strategy; exits never do (they cut risk)."""
        proof = self.proof()
        if proof.get("proven") is not True:
            self.ledger.audit(self.clock(), "entry_blocked_unproven", {"route": route, **proof})
            raise GuardError(
                f"real-money entries are blocked until the '{self.settings.proof_strategy}' "
                f"paper strategy proves profitable: {proof.get('reason', 'no paper record')}"
            )

    def enabled(self) -> None:
        if not self.settings.enabled:
            raise GuardError("execution disabled by server configuration")
        if not self.settings.direct_member:
            raise GuardError("direct-member fee precision must be configured")

    @staticmethod
    def client_id(order: dict[str, Any]) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, order["proposal_id"]))

    async def reconcile(self) -> None:
        for pending in self.ledger.orders(unresolved_only=True):
            try:
                order = await self.broker.find_order(self.client_id(pending), pending["order_id"])
                if order is None:
                    raise GuardError(
                        "order absent from broker reads; absence cannot release reservation"
                    )
                if order["client_order_id"] != self.client_id(pending):
                    raise GuardError("broker client id mismatch")
                intent = Intent.model_validate(pending["intent"])
                fills = await self.broker.order_fills(order, intent)
                self.ledger.reconcile(
                    pending["idempotency_key"],
                    order["order_id"],
                    order["status"],
                    fills,
                    self.clock(),
                )
            except (Exception, asyncio.CancelledError):
                self.ledger.mark(pending["idempotency_key"], "unknown", self.clock())
                raise

    async def refresh(self) -> None:
        try:
            await self.reconcile()
            for ticker in self.ledger.due_outcomes(self.clock()):
                market = await self.broker.market(ticker)
                if market["status"] == "settled":
                    payout = market.get("settlement_value_dollars")
                    if payout is None and market.get("result") in {"yes", "no"}:
                        payout = "1" if market["result"] == "yes" else "0"
                    if payout is not None:
                        self.ledger.record_outcome(ticker, money(payout), self.clock())
            for position in self.ledger.status()["positions"]:
                market = await self.broker.market(position["ticker"])
                if market["status"] == "settled":
                    value = market.get("settlement_value_dollars")
                    if value is None:
                        result = market.get("result")
                        if result not in {"yes", "no"}:
                            raise GuardError("settled market has no final payout")
                        value = "1" if result == "yes" else "0"
                    self.ledger.settle(position["ticker"], money(value), self.clock())
            account = await self.broker.account()
            now = self.clock()
            as_of = account["as_of_ms"]
            if not 0 <= now - as_of <= self.settings.account_max_age_ms:
                raise GuardError("broker account data is stale")
            latest = max((order["created_ms"] for order in self.ledger.orders()), default=0)
            if as_of < latest:
                raise GuardError("broker account data predates the last submission")
            self.ledger.account(as_of, account["balance"], account["positions"])
        except (Exception, asyncio.CancelledError) as error:
            self.ledger.pause(
                self.clock(),
                str(error)
                if isinstance(error, GuardError)
                else "broker reconciliation unavailable",
            )
            raise

    async def status(self, refresh: bool = True) -> dict[str, Any]:
        async with self.lock:
            if refresh:
                with suppress(Exception):
                    await self.refresh()
            return {
                **self.ledger.status(),
                "configured": True,
                "enabled": self.settings.enabled,
                "environment": self.settings.environment,
                "paper_proof": {"strategy": self.settings.proof_strategy, **self.proof()},
                "orders": self.ledger.orders(),
                "scope": (
                    "Caps cover orders routed here; separate manual trades are outside these caps."
                ),
            }

    async def control(self, action: str) -> dict[str, Any]:
        async with self.lock:
            if action == "pause":
                self.ledger.pause(self.clock())
            elif action == "resume":
                self.enabled()
                await self.refresh()
                self.ledger.resume(self.clock(), self.settings.account_max_age_ms)
            elif action == "reconcile":
                await self.refresh()
            else:
                raise GuardError("unknown control")
            return self.ledger.status()

    async def run_reconciliation(self) -> None:
        while True:
            await self.status()
            await asyncio.sleep(5)

    def position(self, ticker: str) -> dict[str, Any] | None:
        return next(
            (row for row in self.ledger.status()["positions"] if row["ticker"] == ticker), None
        )

    async def context(self, request: ReviewRequest) -> dict[str, Any]:
        units = quantity(request.quantity)
        if not 0 < units <= self.settings.max_contracts * 100:
            raise GuardError("quantity must be positive and within server cap")
        account_state = self.ledger.status()
        position = next(
            (row for row in account_state["positions"] if row["ticker"] == request.ticker), None
        )
        initial = self.source(request.ticker, position)
        series = initial["call"]["series"]
        if series not in self.settings.series:
            raise GuardError("series not enabled")
        quote = await self.broker.quote(request.ticker, series)
        snapshot = self.source(request.ticker, position)
        now = self.clock()
        call = snapshot["call"]
        if call["ticker"] != request.ticker or call["series"] != series:
            raise GuardError("market identity changed")
        for label, age in [
            ("engine", now - finite(snapshot["updated_ms"], "engine timestamp")),
            ("Kalshi stream", snapshot.get("ws_age_ms")),
            ("settlement index", call.get("index_age_ms")),
            ("quote", now - finite(quote["observed_ms"], "quote timestamp")),
        ]:
            if not 0 <= finite(age, label) <= self.settings.data_max_age_ms:
                raise GuardError(f"{label} data stale")
        if not 0 <= now - account_state["account_updated_ms"] <= self.settings.account_max_age_ms:
            raise GuardError("account state expired while checking quotes")
        gates = call.get("gates")
        if (
            not isinstance(gates, dict)
            or not gates
            or any(value is not True for value in gates.values())
        ):
            raise GuardError("deterministic data-quality gates failed")
        fair = finite(call.get("fair_up"), "settlement probability")
        raw_model = finite(call.get("model_up"), "model probability")
        if not 0 <= fair <= 1 or not 0 <= raw_model <= 1:
            raise GuardError("invalid settlement probability")
        market = quote["market"]
        if (
            market.get("market_type") != "binary"
            or money(market.get("notional_value_dollars", "0")) != DOLLAR
        ):
            raise GuardError("only binary markets with one-dollar notional are supported")
        if units % 100 and market.get("fractional_trading_enabled") is False:
            raise GuardError("fractional quantity is not enabled for this market")
        left = (timestamp(market["close_time"]) - now) / 1000
        if market["status"] != "active" or not 5 < left <= self.settings.max_seconds_left:
            raise GuardError("market closed or outside model horizon")
        bids = quote["bids"]
        for name, price in [
            ("up_price", DOLLAR - bids["no"][0]),
            ("down_price", DOLLAR - bids["yes"][0]),
        ]:
            if abs(finite(call.get(name), name) - price / DOLLAR) > 0.00000001:
                raise GuardError("engine book and executable quote differ")
        spread = (DOLLAR - bids["yes"][0] - bids["no"][0]) / DOLLAR
        if not 0 < spread <= 0.05:
            raise GuardError("executable spread too wide")
        candidates: dict[str, Any] = {}
        reasons: dict[str, str] = {}
        for side in ("yes", "no"):
            action: Literal["buy", "sell"] = "sell" if position else "buy"
            name = "exit" if position else f"enter_{side}"
            if position and side != position["side"]:
                continue
            price, depth = (
                bids[side]
                if position
                else (
                    DOLLAR - bids["no" if side == "yes" else "yes"][0],
                    bids["no" if side == "yes" else "yes"][1],
                )
            )
            price_string = f"{Decimal(dollars(price)):.4f}"
            if money(price_string) != price:
                raise GuardError("quote exceeds supported four-decimal order precision")
            intent = Intent(
                ticker=request.ticker,
                action=action,
                side=side,
                quantity=request.quantity,
                limit_price=price_string,
            )
            ranges = market.get("price_ranges")
            if not ranges or not any(
                money(item["start"]) <= intent.yes_price <= money(item["end"])
                and (intent.yes_price - money(item["start"])) % money(item["step"]) == 0
                for item in ranges
                if money(item["step"]) > 0
            ):
                raise GuardError("price not on current market tick grid")
            probability = fair if side == "yes" else 1 - fair
            fees_per_contract = fee_reserve(units) / DOLLAR / (units / 100)
            edge = probability - price / DOLLAR - fees_per_contract
            exit_advantage = price / DOLLAR - fees_per_contract - probability
            if depth < units:
                reasons[name] = "insufficient executable top-level depth"
            elif position and position["units"] < units:
                reasons[name] = "exit exceeds actual routed position"
            elif position and exit_advantage < (
                self.settings.exit_advantage + self.settings.latency_buffer
            ):
                reasons[name] = "selling after fees does not clear deterministic exit threshold"
            elif not position and left < self.settings.min_seconds_left:
                reasons[name] = "too close to settlement for entry"
            elif not position and edge < self.settings.min_edge + self.settings.latency_buffer:
                reasons[name] = "estimated edge does not clear fees and latency buffer"
            else:
                candidates[name] = {
                    "intent": intent.model_dump(),
                    "estimated_settlement_probability": probability,
                    "estimated_edge_after_reserved_fees": edge,
                    "exit_advantage_after_reserved_fees": exit_advantage,
                    "maximum_debit": dollars(debit(intent)),
                    "fee_reserve": dollars(fee_reserve(units)),
                    "minimum_exit_proceeds": dollars(
                        intent.price * units // 100 - fee_reserve(units)
                    )
                    if position
                    else None,
                }
        return {
            "ticker": request.ticker,
            "quantity": request.quantity,
            "observed_ms": now,
            "snapshot": snapshot,
            "quote": quote,
            "position": position,
            "seconds_left": left,
            "valid_until_ms": int(
                min(
                    quote["observed_ms"] + self.settings.data_max_age_ms,
                    now - call["index_age_ms"] + self.settings.data_max_age_ms,
                    now - snapshot["ws_age_ms"] + self.settings.data_max_age_ms,
                    account_state["account_updated_ms"] + self.settings.account_max_age_ms,
                )
            ),
            "candidates": candidates,
            "rejections": reasons,
            "allowed_actions": [*candidates, "hold", "skip"],
            "limits": self.settings.limits().model_dump(),
            "account": account_state,
            "probability_note": (
                "Settlement probability comes from the engine. Jev confidence is a decision score."
            ),
        }

    async def review(self, request: ReviewRequest) -> dict[str, Any]:
        async with self.lock:
            self.ledger.audit(self.clock(), "review_requested", request.model_dump())
            self.enabled()
            if self.position(request.ticker) is None:
                self.require_proof("review")
            await self.refresh()
            context = await self.context(request)
            review_id = self.ledger.review(context, self.clock(), self.settings.confirmation_ttl_ms)
            return {"review_id": review_id, "context": context, "submitted": False}

    async def propose(self, request: ProposalRequest) -> dict[str, Any]:
        async with self.lock:
            self.enabled()
            context = self.ledger.get_review(request.review_id, self.clock())
            decision = request.decision
            self.ledger.audit(
                self.clock(),
                "model_decision",
                {
                    "review_id": request.review_id,
                    "decision": decision.model_dump(),
                },
            )
            if decision.mode != "gateway" or decision.model != self.settings.jev_model:
                raise GuardError(
                    "configured gateway model required; fixture cannot authorize orders"
                )
            if decision.action not in context["allowed_actions"]:
                raise GuardError("model action failed deterministic rules")
            if decision.action in {"hold", "skip"}:
                return {
                    "status": decision.action,
                    "submitted": False,
                    "decision": decision.model_dump(),
                }
            if decision.confidence < self.settings.min_model_confidence:
                raise GuardError("model decision confidence below server threshold")
            current = await self.context(
                ReviewRequest(ticker=context["ticker"], quantity=context["quantity"])
            )
            original = context["candidates"][decision.action]
            if current["candidates"].get(decision.action, {}).get("intent") != original["intent"]:
                raise GuardError("market moved during model decision; request a new review")
            return self.ledger.proposal(
                {
                    **original,
                    "decision": decision.model_dump(),
                    "context": context,
                    "review_id": request.review_id,
                },
                self.clock(),
                self.settings.confirmation_ttl_ms,
            )

    async def confirm(self, request: ConfirmRequest) -> dict[str, Any]:
        async with self.lock:
            self.enabled()
            if any(
                order["idempotency_key"] == request.idempotency_key
                for order in self.ledger.orders()
            ):
                return self.ledger.reserve(request, self.clock(), self.settings.account_max_age_ms)[
                    0
                ]
            body = self.ledger.get_proposal(request.proposal_id)
            if request.intent.model_dump() != body["intent"]:
                raise GuardError("confirmed ticker, side, price or quantity changed")
            if request.intent.action == "buy":
                self.require_proof("confirm")
            await self.refresh()
            current = await self.context(
                ReviewRequest(ticker=request.intent.ticker, quantity=request.intent.quantity)
            )
            action = body["decision"]["action"]
            if current["candidates"].get(action, {}).get("intent") != request.intent.model_dump():
                raise GuardError("executable quote or deterministic rules changed; review again")
            self.ledger.audit(
                self.clock(),
                "confirmation_checks",
                {
                    "proposal_id": request.proposal_id,
                    "context": current,
                    "review_to_confirmation_ms": self.clock() - body["context"]["observed_ms"],
                },
            )
            order, fresh = self.ledger.reserve(
                request,
                self.clock(),
                self.settings.account_max_age_ms,
                validation_expires_ms=current["valid_until_ms"],
                clock=self.clock,
            )
            if not fresh:
                return order
            key = request.idempotency_key
            try:
                receipt = await self.broker.submit(
                    request.intent,
                    self.client_id(order),
                    min(current["valid_until_ms"], body["expires_ms"]),
                )
            except BrokerRejectedError as error:
                self.ledger.mark(
                    key, "rejected", self.clock(), local_rejection=not error.dispatched
                )
                self.ledger.audit(
                    self.clock(),
                    "broker_rejection" if error.dispatched else "local_rejection",
                    {"key": key, "reason": str(error)},
                )
            except (Exception, asyncio.CancelledError) as error:
                self.ledger.mark(key, "unknown", self.clock())
                self.ledger.pause(
                    self.clock(), "submission outcome unknown; reconciliation required"
                )
                if isinstance(error, asyncio.CancelledError):
                    raise
            else:
                self.ledger.mark(key, "accepted", self.clock(), receipt["order_id"])
                self.ledger.audit(self.clock(), "broker_receipt", {"key": key, "receipt": receipt})
                with suppress(Exception):
                    await self.refresh()
            return next(order for order in self.ledger.orders() if order["idempotency_key"] == key)
