"""Attach one execution owner to the recorder and its durable local volume."""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import os
import socket
from pathlib import Path
from typing import IO, Any

from edgebook.engine.decide import Position, evaluate
from edgebook.engine.loop import LiveEngine
from edgebook.execution.broker import KalshiBroker
from edgebook.execution.live_ledger import GuardError, LiveSettings
from edgebook.execution.proof import paper_proof
from edgebook.execution.service import ExecutionService
from edgebook.execution.sqlite_ledger import SQLiteLedger
from edgebook.venues.kalshi.auth import KalshiSigner


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
            KalshiSigner.from_pem(key_id, pem), settings.environment, settings.subaccount
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

        service = ExecutionService(settings, ledger, broker, source, engine.clock, proof=proof)
        return service, broker, owner
    except BaseException:
        owner.close()
        raise
