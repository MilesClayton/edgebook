"""Attach one execution owner to the recorder and its durable local volume."""

from __future__ import annotations

import asyncio
import dataclasses
import fcntl
import hashlib
import os
import socket
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import IO, Any

import httpx

from edgebook.engine.decide import Position, evaluate
from edgebook.engine.loop import LiveEngine
from edgebook.execution.broker import KalshiBroker
from edgebook.execution.live_ledger import GuardError, LiveSettings
from edgebook.execution.longshot import longshot_quotes
from edgebook.execution.proof import paper_proof
from edgebook.execution.service import ExecutionService
from edgebook.execution.sqlite_ledger import SQLiteLedger
from edgebook.venues.kalshi.auth import KalshiSigner


async def gateway_decision(
    client: httpx.AsyncClient, url: str, token: str, context: dict[str, Any]
) -> dict[str, Any]:
    """Retry inference once on a temporary failure; this endpoint cannot send orders."""
    for attempt in range(2):
        try:
            response = await client.post(
                url, json={"context": context}, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.TransportError:
            if attempt == 0:
                await asyncio.sleep(0.25)
                continue
            raise GuardError("Jev connection timed out; will retry next check") from None
        if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
            await asyncio.sleep(0.25)
            continue
        if response.is_error:
            explanations = {
                401: "Jev connection authentication failed; check the server proposal token",
                403: "Jev endpoint access was denied",
                402: "Jev gateway credits are exhausted",
                404: "Jev endpoint or model was not found",
                429: "Jev is rate limited; will retry next check",
            }
            message = explanations.get(
                response.status_code, "Jev provider is unavailable; will retry next check"
            )
            # Only our endpoint's structured, credential-free failure description
            # is used. Never display raw provider responses or request bodies.
            if response.status_code == 502:
                with suppress(ValueError):
                    body = response.json()
                    if isinstance(body, dict) and body.get("code") in {
                        "jev_auth",
                        "jev_credits",
                        "jev_rate_limit",
                        "jev_timeout",
                        "jev_invalid_answer",
                    }:
                        message = str(body.get("error") or message)
            raise GuardError(f"{message} (HTTP {response.status_code})")
        try:
            body = response.json()
        except ValueError:
            raise GuardError("Jev returned an invalid answer; no order authorized") from None
        if not isinstance(body, dict):
            raise GuardError("Jev returned an invalid answer; no order authorized")
        return body
    raise GuardError("Jev unavailable; no order authorized")


def claim_owner(directory: Path, settings: LiveSettings) -> IO[str]:
    if settings.topology != "single_host_durable_sqlite":
        raise GuardError("single_host_durable_sqlite topology must be explicitly configured")
    mount = Path(settings.durable_mount).resolve()
    if not settings.durable_mount or not os.path.ismount(mount):
        raise GuardError("execution durable mount is not a mounted persistent volume")
    if not directory.resolve().is_relative_to(mount) or mount == Path("/"):
        raise GuardError("ledger must be inside the configured persistent volume")
    actual_host = os.environ.get("FLY_MACHINE_ID") or socket.gethostname()
    if not settings.host_id or actual_host != settings.host_id:
        raise GuardError("this host is not the explicitly configured execution owner")
    directory.mkdir(parents=True, exist_ok=True)
    owner = (directory / "ledger.sqlite3.lock").open("a+")
    try:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        owner.close()
        raise GuardError("another execution process already owns this ledger") from None
    return owner


def build_service(
    directory: Path, settings: LiveSettings, engine: LiveEngine, key_id: str, pem: bytes
) -> tuple[ExecutionService, KalshiBroker, IO[str]]:
    if not settings.direct_member:
        raise GuardError("configure a direct Kalshi member account for the fee bound")
    if (
        len(settings.api_token) < 32
        or len(settings.proposal_token) < 32
        or settings.api_token == settings.proposal_token
    ):
        raise GuardError(
            "distinct operator and proposal server tokens of at least 32 characters required"
        )
    owner = claim_owner(directory, settings)
    try:
        binding = hashlib.sha256(
            f"{settings.environment}:{key_id}:{settings.subaccount}:{settings.host_id}".encode()
        ).hexdigest()
        ledger = SQLiteLedger(directory / "ledger.sqlite3", settings.limits(), binding)
        ledger.recover(engine.clock())
        broker = KalshiBroker(
            KalshiSigner.from_pem(key_id, pem),
            settings.environment,
            settings.subaccount,
            exchange_index=settings.exchange_index,
        )

        def source(ticker: str, holding: dict[str, Any] | None) -> dict[str, Any]:
            market = engine.state.markets.get(ticker)
            if market is None:
                raise GuardError("ticker is not in the live engine")
            position = (
                None
                if holding is None
                else Position(
                    holding["side"],
                    holding["units"] / 100,
                    holding["cost"] / 100_000_000 / (holding["units"] / 100),
                )
            )
            now = engine.clock()
            call = evaluate(engine.state, market, now, engine.cfg, position)
            return {
                "updated_ms": now,
                "ws_age_ms": now - engine.state.ws_last_ms,
                "call": dataclasses.asdict(call),
                "market_flow": engine.state.flow(ticker, now),
            }

        proven = next((r for r in engine.runners if r.spec.name == settings.proof_strategy), None)
        if proven is None or proven is not engine.primary:
            raise GuardError(
                "proof_strategy must name the engine's primary strategy (execution uses its prices)"
            )

        def proof() -> dict[str, Any]:
            return paper_proof(
                proven.account.closed, settings.proof_min_trades, settings.proof_min_days
            )

        def tickers() -> list[str]:
            """Open markets of the autopilot series with enough time left to enter."""
            now_s = engine.clock() / 1000
            return sorted(
                m.ticker
                for m in engine.state.markets.values()
                if m.series in settings.series
                and m.open_ts <= now_s
                and m.close_ts - now_s > settings.min_seconds_left
            )

        decide = None
        if settings.jev_decide_url:
            if not settings.jev_decide_url.startswith("https://"):
                raise GuardError("jev_decide_url must be https")
            jev_client = httpx.AsyncClient(timeout=15.0, follow_redirects=False)

            async def decide(context: dict[str, Any]) -> dict[str, Any]:
                """Ask Jev (on Vercel, via AI Gateway) for a bounded decision."""
                return await gateway_decision(
                    jev_client, settings.jev_decide_url, settings.proposal_token, context
                )

        seller = next(
            (
                r
                for r in engine.runners
                if r.spec.name == settings.longshot_strategy and r.spec.mode == "longshot"
            ),
            None,
        )

        sales: Callable[[], list[dict[str, Any]]] | None = None
        seller_proof: Callable[[], dict[str, Any]] | None = None
        if seller is not None:
            paper = seller

            def sales() -> list[dict[str, Any]]:
                return longshot_quotes(
                    paper.calls,
                    engine.state.markets,
                    paper.spec.longshot,
                    settings.series,
                    engine.clock(),
                    paper.last_tick_ms,
                    settings.data_max_age_ms,
                )

            def seller_proof() -> dict[str, Any]:
                return paper_proof(
                    paper.account.closed, settings.proof_min_trades, settings.proof_min_days
                )

        service = ExecutionService(
            settings,
            ledger,
            broker,
            source,
            engine.clock,
            proof=proof,
            decide=decide,
            tickers=tickers if decide is not None else None,
            longshot_quotes=sales,
            longshot_proof=seller_proof,
        )
        return service, broker, owner
    except BaseException:
        owner.close()
        raise
