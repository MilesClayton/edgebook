"""Authoritative review, confirmation, risk, and reconciliation workflow."""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from decimal import Decimal
from typing import Any, Literal, Protocol

import httpx

from edgebook.execution.broker import BrokerRejectedError, timestamp
from edgebook.execution.live_ledger import (
    DOLLAR,
    ConfirmRequest,
    GuardError,
    Intent,
    Limits,
    LiveSettings,
    ModelDecision,
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
        self,
        intent: Intent,
        client_id: str,
        deadline_ms: int | None = None,
        resting_until_s: int | None = None,
    ) -> dict[str, Any]: ...
    async def cancel(self, order_id: str) -> None: ...
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
        decide: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        tickers: Callable[[], list[str]] | None = None,
        longshot_quotes: Callable[[], list[dict[str, Any]]] | None = None,
        longshot_proof: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.settings, self.ledger, self.broker = settings, ledger, broker
        self.source, self.clock = source, clock
        self.proof = proof  # the paper record of the strategy whose prices execution uses
        self.decide = decide  # Jev, for autopilot (None: autopilot unavailable)
        self.tickers = tickers  # markets autopilot may trade right now
        self.autopilot_last: list[dict[str, Any]] = []
        # long-shot pilot: the paper strategy's current sales, and its paper record
        self.longshot_quotes = longshot_quotes
        self.longshot_proof = longshot_proof
        self.longshot_last: list[dict[str, Any]] = []
        self.lock = asyncio.Lock()

    def require_proof(self, route: str, longshot: bool = False) -> None:
        """New entries need a paper-proven strategy; exits never do (they cut risk).

        The owner may waive the gate; every entry checked while waived is audited with the
        current (unproven) paper record so the decision stays on file."""
        source = self.longshot_proof if longshot else self.proof
        name = self.settings.longshot_strategy if longshot else self.settings.proof_strategy
        proof = source() if source is not None else {"proven": False, "reason": "no paper record"}
        if not self.settings.require_paper_proof:
            self.ledger.audit(self.clock(), "paper_proof_waived", {"route": route, **proof})
            return
        if proof.get("proven") is not True:
            self.ledger.audit(self.clock(), "entry_blocked_unproven", {"route": route, **proof})
            raise GuardError(
                f"real-money entries are blocked until the '{name}' "
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
            self.ledger.account(
                as_of,
                account["balance"],
                account["positions"],
                account.get("resting_client_ids", ()),
            )
        except (Exception, asyncio.CancelledError) as error:
            temporary = (
                isinstance(error, (httpx.TransportError, OSError, TimeoutError))
                or (
                    isinstance(error, httpx.HTTPStatusError)
                    and (error.response.status_code == 429 or error.response.status_code >= 500)
                )
                or (
                    isinstance(error, GuardError)
                    and str(error)
                    in {
                        "broker account data is stale",
                        "broker account data predates the last submission",
                    }
                )
            ) and not self.ledger.status()["unknown_or_pending"]
            self.ledger.audit(
                self.clock(),
                "reconciliation_failed",
                {
                    "error_type": type(error).__name__,
                    "temporary": temporary,
                    "http_status": error.response.status_code
                    if isinstance(error, httpx.HTTPStatusError)
                    else None,
                },
            )
            self.ledger.pause(
                self.clock(),
                "Temporary broker connection issue; retrying automatically"
                if temporary
                else str(error)
                if isinstance(error, GuardError)
                else "broker reconciliation unavailable",
                recoverable=temporary,
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
                "paper_proof": {
                    "strategy": self.settings.proof_strategy,
                    "required": self.settings.require_paper_proof,
                    **self.proof(),
                },
                "orders": self.ledger.orders(),
                "autopilot_available": self.decide is not None and self.tickers is not None,
                "autopilot_last": self.autopilot_last,
                "autopilot_every_s": self.settings.autopilot_every_s,
                "longshot_available": self.longshot_quotes is not None,
                "longshot_last": self.longshot_last,
                "longshot_paper_proof": {
                    "strategy": self.settings.longshot_strategy,
                    **(self.longshot_proof() if self.longshot_proof is not None else {}),
                },
                "scope": (
                    "Caps cover orders routed here; separate manual trades are outside these caps."
                ),
            }

    async def control(self, action: str) -> dict[str, Any]:
        async with self.lock:
            if action == "pause":
                self.ledger.pause(self.clock())
                await self.cancel_resting("paused")
            elif action == "resume":
                self.enabled()
                await self.refresh()
                self.ledger.resume(self.clock(), self.settings.account_max_age_ms)
            elif action == "reconcile":
                await self.refresh()
            else:
                raise GuardError("unknown control")
            return self.ledger.status()

    async def set_limits(self, limits: Limits) -> dict[str, Any]:
        """Owner changes the money caps (audited; spend and loss so far are kept)."""
        async with self.lock:
            self.ledger.set_limits(limits, self.clock())
            return self.ledger.status()

    async def set_budget(self, cents: int) -> dict[str, Any]:
        """A reusable capital allowance; realized losses and open risk consume it."""
        limits = Limits(
            capital_budget_cents=cents,
            purchase_spend_cents=cents,
            open_exposure_cents=cents,
            realized_loss_cents=cents,
            order_debit_cents=cents,
            max_contracts=10000,
        )
        async with self.lock:
            # Saving an allowance changes strategy permissions. It must not
            # start an already-enabled autopilot without the owner's Start.
            self.ledger.pause(self.clock(), "budget saved; press Start to trade")
            await self.cancel_resting("budget changed")
            self.ledger.set_limits(limits, self.clock())
            return self.ledger.status()

    async def start(self, strategy: str, generation: int) -> dict[str, Any]:
        """Atomically configure and start, rejecting a Start overtaken by Stop."""
        async with self.lock:
            state = self.ledger.status()
            if state["generation"] != generation:
                raise GuardError("controls changed; refresh before starting again")
            if not state["limits"].get("capital_budget_cents"):
                raise GuardError("save a trading budget first")
            if strategy not in {"autopilot", "longshot", "both"}:
                raise GuardError("unknown trading approach")
            self.enabled()
            if strategy != "longshot" and (self.decide is None or self.tickers is None):
                raise GuardError("Jev is not configured on this server")
            if strategy != "autopilot" and self.longshot_quotes is None:
                raise GuardError("long-shot is not configured on this server")
            self.ledger.pause(self.clock(), "preparing automatic trading")
            await self.cancel_resting("trading approach changed")
            await self.refresh()
            if self.resting():
                self.ledger.pause(self.clock(), "resting order cancellation not confirmed")
                raise GuardError("resting order cancellation not confirmed; refresh before Start")
            self.ledger.set_autopilot(strategy != "longshot", self.clock())
            self.ledger.set_autopilot(strategy != "autopilot", self.clock(), kind="longshot")
            self.ledger.resume(self.clock(), self.settings.account_max_age_ms)
            return self.ledger.status()

    async def set_autopilot(self, enabled: bool) -> dict[str, Any]:
        async with self.lock:
            if enabled:
                self.enabled()
                if self.decide is None or self.tickers is None:
                    raise GuardError(
                        "autopilot is not configured on this server (Jev decision URL)"
                    )
            self.ledger.set_autopilot(enabled, self.clock())
            return self.ledger.status()

    async def set_longshot(self, enabled: bool) -> dict[str, Any]:
        async with self.lock:
            if enabled:
                self.enabled()
                if self.longshot_quotes is None:
                    raise GuardError("the long-shot pilot is not configured on this server")
            self.ledger.set_autopilot(enabled, self.clock(), kind="longshot")
            if not enabled:
                await self.cancel_resting("long-shot pilot switched off")
            return self.ledger.status()

    def resting(self) -> list[dict[str, Any]]:
        return [o for o in self.ledger.orders(unresolved_only=True) if o["resting"]]

    async def cancel_resting(
        self, reason: str, keep: Callable[[dict[str, Any]], bool] = lambda o: False
    ) -> list[dict[str, Any]]:
        """Cancel our resting orders (all, or those `keep` rejects), then reconcile. A cancel
        that fails pauses execution; Kalshi still expires the order before close."""
        notes = []
        for order in self.resting():
            if keep(order):
                continue
            note = {"ticker": order["intent"]["ticker"], "at_ms": self.clock()}
            try:
                if order["order_id"]:
                    await self.broker.cancel(order["order_id"])
                note["outcome"] = f"canceled: {reason}"
            except Exception as error:
                note["outcome"] = f"cancel failed: {type(error).__name__}"
                self.ledger.pause(self.clock(), "resting order cancel failed; check Kalshi")
            self.ledger.audit(self.clock(), "longshot_cancel", note)
            notes.append(note)
        if notes:
            with suppress(Exception):
                await self.refresh()
        return notes

    async def longshot_step(self) -> list[dict[str, Any]]:
        """One pilot pass: mirror the paper long-shot seller's current sales with small real
        post-only bids. Orders it no longer wants are canceled first; new ones need fresh
        account data, the live book to match, and room under every cap."""
        if self.longshot_quotes is None:
            return []
        async with self.lock:
            state = self.ledger.status()
            on = (
                state["longshot"]
                and self.settings.enabled
                and not state["paused"]
                and not state["unknown_or_pending"]
            )
            wanted = {q["ticker"]: q for q in self.longshot_quotes()} if on else {}

            def still_wanted(order: dict[str, Any]) -> bool:
                q = wanted.get(order["intent"]["ticker"])
                return q is not None and order["intent"] == self._longshot_intent(q).model_dump()

            notes = await self.cancel_resting("the paper strategy no longer sells it", still_wanted)
            if on:
                notes += await self._longshot_place(wanted)
            if notes:
                self.longshot_last = notes[-50:]
            return notes

    def _longshot_intent(self, q: dict[str, Any]) -> Intent:
        return Intent(
            ticker=q["ticker"],
            action="buy",
            side=q["side"],
            quantity=self.settings.longshot_contracts,
            limit_price=f"{Decimal(dollars(q['price'])):.4f}",
        )

    async def _longshot_place(self, wanted: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = []
        held = {p["ticker"] for p in self.ledger.status()["positions"]}
        working = {o["intent"]["ticker"] for o in self.resting()}
        for q in sorted(wanted.values(), key=lambda q: -q["seconds_left"]):
            ticker = q["ticker"]
            if ticker in held or ticker in working:
                continue  # never adds to a held market; one order per market
            note: dict[str, Any] = {"ticker": ticker, "at_ms": self.clock(), "side": q["side"]}
            notes.append(note)
            if len(working) >= self.settings.longshot_max_orders:
                note["outcome"] = "skipped: resting-order limit reached"
                break
            try:
                await self._longshot_order(q, note)
                if note["outcome"].startswith("resting"):
                    working.add(ticker)
            except GuardError as error:
                note["outcome"] = f"refused: {error}"
            self.ledger.audit(self.clock(), "longshot_order", note)
        return notes

    async def _longshot_order(self, q: dict[str, Any], note: dict[str, Any]) -> None:
        ticker = q["ticker"]
        intent = self._longshot_intent(q)
        event = ticker.rsplit("-", 1)[0]
        if self._event_commitment(event) + debit(intent) > (
            self.settings.longshot_event_cents * DOLLAR // 100
        ):
            raise GuardError("per-close limit reached for this hour")
        expires_s = q["close_ms"] // 1000 - self.settings.longshot_expire_s
        if expires_s - self.clock() / 1000 < 30:
            raise GuardError("too close to the pilot's cut-off before close")
        self.require_proof("longshot", longshot=True)
        await self.refresh()
        quote = await self.broker.quote(ticker, q["series"])
        if self.longshot_quotes is None or not any(
            fresh["ticker"] == ticker and self._longshot_intent(fresh) == intent
            for fresh in self.longshot_quotes()
        ):
            raise GuardError("long-shot data expired or signal changed during quote refresh")
        market = quote["market"]
        if market["status"] != "active":
            raise GuardError("market not active")
        if quote["bids"][q["side"]][0] != intent.price:
            raise GuardError("live book moved; the paper price is no longer the best bid")
        if not any(
            money(r["start"]) <= intent.yes_price <= money(r["end"])
            and (intent.yes_price - money(r["start"])) % money(r["step"]) == 0
            for r in market.get("price_ranges") or []
            if money(r["step"]) > 0
        ):
            raise GuardError("price not on current market tick grid")
        key = f"longshot-{uuid.uuid4().hex}"
        order = self.ledger.reserve_resting(
            key,
            intent,
            {"quote": q, "observed_ms": quote["observed_ms"]},
            self.clock(),
            self.settings.account_max_age_ms,
        )
        try:
            receipt = await self.broker.submit(
                intent,
                self.client_id(order),
                quote["observed_ms"] + self.settings.data_max_age_ms,
                resting_until_s=expires_s,
            )
        except BrokerRejectedError as error:
            self.ledger.mark(key, "rejected", self.clock(), local_rejection=not error.dispatched)
            note["outcome"] = f"rejected: {error}"
            return
        except (Exception, asyncio.CancelledError):
            self.ledger.mark(key, "unknown", self.clock())
            self.ledger.pause(self.clock(), "submission outcome unknown; reconciliation required")
            raise
        self.ledger.mark(key, "accepted", self.clock(), receipt["order_id"])
        note["outcome"] = (
            f"resting {intent.side.upper()} bid {intent.limit_price} x {intent.quantity}"
        )
        with suppress(Exception):
            await self.refresh()

    def _event_commitment(self, event: str) -> int:
        """Money at risk on every strike of one close: held cost plus resting reservations."""
        held = sum(
            p["cost"]
            for p in self.ledger.status()["positions"]
            if p["ticker"].rsplit("-", 1)[0] == event
        )
        resting = sum(
            max(0, money(o["reserved_debit"]) - money(o["fill_cost"]) - money(o["fees"]))
            for o in self.resting()
            if o["intent"]["ticker"].rsplit("-", 1)[0] == event
        )
        return int(held) + resting

    async def run_longshot(self) -> None:
        while True:
            with suppress(Exception):
                await self.longshot_step()
            await asyncio.sleep(self.settings.longshot_every_s)

    async def autopilot_step(self) -> list[dict[str, Any]]:
        """One autopilot pass: for each tradable market, review it, ask Jev only if the
        engine has a candidate, and confirm Jev's choice through the normal checks."""
        state = self.ledger.status()
        if (
            not state.get("autopilot")
            or state["paused"]
            or state["unknown_or_pending"]
            or not self.settings.enabled
            or self.decide is None
            or self.tickers is None
        ):
            return []
        # Manage every routed holding, even hourly markets and markets too close
        # to settlement for a new entry. Extra browser tabs are unnecessary.
        managed = [p["ticker"] for p in state["positions"]]
        notes = []
        for ticker in dict.fromkeys([*managed, *self.tickers()]):
            current = self.ledger.status()
            if current["paused"] or not current["autopilot"]:
                break
            notes.append(await self._autopilot_market(ticker))
        if notes:
            self.autopilot_last = notes
        return notes

    async def _autopilot_market(self, ticker: str) -> dict[str, Any]:
        note: dict[str, Any] = {"ticker": ticker, "at_ms": self.clock()}
        holding = self.position(ticker)
        try:
            review = await self.review(
                ReviewRequest(ticker=ticker, quantity=self.settings.autopilot_contracts),
                automatic=True,
            )
            context = review["context"]
            note["engine"] = self._engine_view(context["snapshot"], context["rejections"])
            usable = [
                a for a in context["candidates"] if holding is not None or a.startswith("enter")
            ]
            if not usable:
                note["outcome"] = "no trade: engine sees no edge after fees"
                return note
            if self.decide is None:
                raise GuardError("autopilot is not configured")
            decision = ModelDecision.model_validate(await self.decide(context))
            note["jev"] = {"action": decision.action, "confidence": decision.confidence}
            proposal = await self.propose(
                ProposalRequest(review_id=review["review_id"], decision=decision)
            )
            if proposal.get("status") != "confirmation_required":
                note["outcome"] = f"Jev chose {decision.action}: no order"
                return note
            intent = Intent.model_validate(proposal["intent"])
            if intent.action == "buy" and holding is not None:
                note["outcome"] = "already holding this market: autopilot never adds"
                return note
            order = await self.confirm(
                ConfirmRequest(
                    proposal_id=proposal["proposal_id"],
                    confirmation_token=proposal["confirmation_token"],
                    idempotency_key=f"autopilot-{uuid.uuid4().hex}",
                    intent=intent,
                    confirmed=True,
                )
            )
            note["outcome"] = f"order {order.get('status')}"
            note["order"] = {
                k: order.get(k)
                for k in ("intent", "status", "filled_quantity", "fill_cost", "fees")
            }
            self.ledger.audit(self.clock(), "autopilot_order", note)
        except GuardError as error:
            note["outcome"] = f"refused: {error}"
            if "engine" not in note:
                with suppress(Exception):
                    note["engine"] = self._engine_view(self.source(ticker, holding), {})
        except Exception as error:  # Jev unreachable, malformed answer, etc.: no order
            note["outcome"] = f"error: {type(error).__name__}"
        return note

    def _engine_view(self, snapshot: dict[str, Any], rejections: dict[str, str]) -> dict[str, Any]:
        """What the engine saw, in the popup's words: prices, its estimate and the edge."""
        call = snapshot["call"]
        flow = snapshot.get("market_flow") or {}
        return {
            "seconds_left": call.get("seconds_left"),
            "fair_up": call.get("fair_up"),
            "up_price": call.get("up_price"),
            "down_price": call.get("down_price"),
            "edge_up": call.get("edge_up"),
            "edge_down": call.get("edge_down"),
            "edge_needed": self.settings.min_edge + self.settings.latency_buffer,
            "reason": call.get("reason"),
            "failed_gates": [k for k, v in (call.get("gates") or {}).items() if v is not True],
            "rejections": rejections,
            "share_bought_up_5m": (flow.get("tape") or {})
            .get("last_5m", {})
            .get("share_bought_up"),
            "index_change_5m_bps": (flow.get("momentum") or {}).get("index_change_5m_bps"),
        }

    async def run_autopilot(self) -> None:
        while True:
            with suppress(Exception):
                await self.autopilot_step()
            await asyncio.sleep(self.settings.autopilot_every_s)

    async def run_reconciliation(self) -> None:
        while True:
            try:
                async with self.lock:
                    await self.refresh()
                    if self.settings.enabled:
                        self.ledger.recover_if_ready(self.clock(), self.settings.account_max_age_ms)
            except Exception as error:
                # refresh records and classifies the failure. Keep checking;
                # one unavailable broker read must not kill this background task.
                logging.getLogger(__name__).warning(
                    "Recovery check failed: %s", type(error).__name__
                )
            await asyncio.sleep(5)

    def position(self, ticker: str) -> dict[str, Any] | None:
        return next(
            (row for row in self.ledger.status()["positions"] if row["ticker"] == ticker), None
        )

    async def context(
        self, request: ReviewRequest, *, automatic: bool = False, resize: bool = False
    ) -> dict[str, Any]:
        units = quantity(request.quantity)
        account_state = self.ledger.status()
        limits = Limits.model_validate(account_state["limits"])
        discretionary = automatic and limits.capital_budget_cents is not None
        if automatic and (not account_state["autopilot"] or account_state["paused"]):
            raise GuardError("autopilot stopped; review again after Start")
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
        if not isinstance(gates, dict) or not gates:
            raise GuardError("deterministic data-quality gates failed")
        failed = [str(name) for name, value in gates.items() if value is not True]
        if failed:
            raise GuardError("deterministic data-quality gates failed: " + ", ".join(failed))
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
        left = (timestamp(market["close_time"]) - now) / 1000
        if market["status"] != "active" or not 5 < left <= self.settings.max_seconds_left:
            raise GuardError("market closed or outside model horizon")
        bids = quote["bids"]
        if resize:
            if position:
                units = min(
                    position["units"], limits.max_contracts * 100, bids[position["side"]][1]
                )
            elif limits.capital_budget_cents is not None:
                room = min(
                    money(account_state["remaining_purchase_budget"]),
                    money(account_state["available_balance"]),
                    limits.order_debit_cents * DOLLAR // 100,
                )
                units = limits.max_contracts * 100
            else:
                units = min(units, limits.max_contracts * 100)
        if not 0 < units <= limits.max_contracts * 100:
            raise GuardError("not enough budget or available contracts for this order")
        if units % 100 and market.get("fractional_trading_enabled") is False:
            raise GuardError("fractional quantity is not enabled for this market")
        amount = f"{Decimal(units) / 100:.2f}"
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
            candidate_units = units
            if resize and not position and limits.capital_budget_cents is not None:
                candidate_units = min(
                    units, (room // (price + fee_reserve(100))) * 100, (depth // 100) * 100
                )
            if candidate_units <= 0:
                reasons[name] = "not enough budget or executable depth for one contract"
                continue
            amount = f"{Decimal(candidate_units) / 100:.2f}"
            if money(price_string) != price:
                raise GuardError("quote exceeds supported four-decimal order precision")
            intent = Intent(
                ticker=request.ticker,
                action=action,
                side=side,
                quantity=amount,
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
            fees_per_contract = fee_reserve(candidate_units) / DOLLAR / (candidate_units / 100)
            edge = probability - price / DOLLAR - fees_per_contract
            exit_advantage = price / DOLLAR - fees_per_contract - probability
            if depth < candidate_units:
                reasons[name] = "insufficient executable top-level depth"
            elif position and position["units"] < candidate_units:
                reasons[name] = "exit exceeds actual routed position"
            elif (
                position
                and not discretionary
                and exit_advantage < (self.settings.exit_advantage + self.settings.latency_buffer)
            ):
                reasons[name] = "selling after fees does not clear deterministic exit threshold"
            elif not position and left < self.settings.min_seconds_left:
                reasons[name] = "too close to settlement for entry"
            elif (
                not position
                and not discretionary
                and edge < self.settings.min_edge + self.settings.latency_buffer
            ):
                reasons[name] = "estimated edge does not clear fees and latency buffer"
            else:
                candidates[name] = {
                    "intent": intent.model_dump(),
                    "estimated_settlement_probability": probability,
                    "estimated_edge_after_reserved_fees": edge,
                    "exit_advantage_after_reserved_fees": exit_advantage,
                    "maximum_debit": dollars(debit(intent)),
                    "fee_reserve": dollars(fee_reserve(candidate_units)),
                    "minimum_exit_proceeds": dollars(
                        intent.price * candidate_units // 100 - fee_reserve(candidate_units)
                    )
                    if position
                    else None,
                }
        return {
            "ticker": request.ticker,
            "quantity": amount,
            "automatic": automatic,
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
            "limits": limits.model_dump(),
            "account": account_state,
            "probability_note": (
                "Settlement probability comes from the engine. Jev confidence is a decision score."
            ),
        }

    async def review(self, request: ReviewRequest, *, automatic: bool = False) -> dict[str, Any]:
        async with self.lock:
            self.ledger.audit(self.clock(), "review_requested", request.model_dump())
            self.enabled()
            if self.position(request.ticker) is None:
                self.require_proof("review")
            await self.refresh()
            context = await self.context(request, automatic=automatic, resize=automatic)
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
            automatic = context.get("automatic", False)
            amount = context["candidates"][decision.action]["intent"]["quantity"]
            if automatic:
                maximum = quantity(amount)
                selected = (
                    min(100, maximum)
                    if decision.size == "small"
                    else max(1, maximum // 2)
                    if decision.size == "medium"
                    else maximum
                )
                # Whole-contract entries; exits may reduce fractional fills.
                if not context["position"]:
                    selected = max(100, selected // 100 * 100)
                amount = f"{Decimal(selected) / 100:.2f}"
            current = await self.context(
                ReviewRequest(ticker=context["ticker"], quantity=amount), automatic=automatic
            )
            original = context["candidates"][decision.action]
            expected = {**original["intent"], "quantity": amount}
            if current["candidates"].get(decision.action, {}).get("intent") != expected:
                raise GuardError("market moved during model decision; request a new review")
            return self.ledger.proposal(
                {
                    **current["candidates"][decision.action],
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
                ReviewRequest(ticker=request.intent.ticker, quantity=request.intent.quantity),
                automatic=body["context"].get("automatic", False),
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
