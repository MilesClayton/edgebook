from __future__ import annotations

import socket
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from edgebook.engine.loop import LiveEngine
from edgebook.engine.state import LiveState
from edgebook.execution.live_ledger import GuardError
from edgebook.execution.runtime import build_service, claim_owner
from edgebook.execution.sqlite_ledger import SQLiteLedger
from edgebook.venues.kalshi.models import RestMarket
from tests.conftest import market
from tests.execution_helpers import NOW, TICKER, live_settings


def test_owner_requires_explicit_durable_mount_and_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = live_settings(durable_mount=str(tmp_path), host_id="fixture")
    monkeypatch.setenv("FLY_MACHINE_ID", "fixture")
    with pytest.raises(GuardError, match="topology"):
        claim_owner(tmp_path, settings.model_copy(update={"topology": "disabled"}))
    with pytest.raises(GuardError, match="mounted"):
        claim_owner(tmp_path, settings)
    monkeypatch.setattr("os.path.ismount", lambda _: True)
    with pytest.raises(GuardError, match="inside"):
        claim_owner(tmp_path.parent, settings)
    with pytest.raises(GuardError, match="owner"):
        claim_owner(tmp_path, settings.model_copy(update={"host_id": "other"}))
    lock = claim_owner(tmp_path, settings)
    try:
        with pytest.raises(GuardError, match="another"):
            claim_owner(tmp_path, settings)
    finally:
        lock.close()
    monkeypatch.delenv("FLY_MACHINE_ID")
    local = settings.model_copy(update={"host_id": socket.gethostname()})
    claim_owner(tmp_path, local).close()


async def test_build_disabled_config_recovery_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLY_MACHINE_ID", "fixture")
    monkeypatch.setattr("os.path.ismount", lambda _: True)
    settings = live_settings(durable_mount=str(tmp_path), host_id="fixture").model_copy(
        update={"proof_strategy": "v1"}  # the single-strategy engine's primary
    )
    engine = LiveEngine(LiveState(), tmp_path / "paper.json", clock=lambda: NOW)
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    for updates in (
        {"direct_member": False},
        {"api_token": ""},
        {"proposal_token": settings.api_token},
        {"proof_strategy": "maker"},  # not a strategy this engine runs / not its primary
    ):
        with pytest.raises(GuardError):
            build_service(tmp_path, settings.model_copy(update=updates), engine, "fixture", pem)
    with pytest.raises(ValueError, match="load PEM"):
        build_service(tmp_path, settings, engine, "fixture", b"invalid-pem")
    service, broker, owner = build_service(tmp_path, settings, engine, "fixture", pem)
    try:
        assert service.ledger.status()["paused"]
        proof = service.proof()
        assert proof["proven"] is False  # an empty paper account proves nothing
        assert proof["min_trades"] == 200
        with pytest.raises(GuardError, match="ticker"):
            service.source("ABSENT", None)
        engine.state.set_market(RestMarket.model_validate(market(TICKER)), "KXBTC15M")
        snapshot = service.source(TICKER, {"side": "yes", "units": 37, "cost": 15_300_000})
        assert snapshot["call"]["position"]["contracts"] == 0.37
        assert snapshot["call"]["position"]["avg_cost"] == pytest.approx(0.153 / 0.37)
        service.settings = settings.model_copy(update={"direct_member": False})
        with pytest.raises(GuardError, match="direct-member"):
            service.enabled()
        with pytest.raises(GuardError, match="identity"):
            SQLiteLedger(tmp_path / "ledger.sqlite3", settings.limits(), "different")
    finally:
        await broker.close()
        owner.close()
