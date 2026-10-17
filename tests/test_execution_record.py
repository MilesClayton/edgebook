from __future__ import annotations

import secrets
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from edgebook.app import cli
from edgebook.config import Settings
from edgebook.execution.live_ledger import LiveSettings
from edgebook.recorder import http as dashboard_http
from edgebook.venues.kalshi.auth import KalshiSigner


@pytest.mark.parametrize("mode", ["disabled", "missing_credentials", "configured"])
async def test_recorder_attaches_execution_and_cleans_up_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signer: KalshiSigner, mode: str
) -> None:
    recorder = MagicMock()
    recorder.background = []
    recorder.run = AsyncMock()
    runner = MagicMock()
    runner.cleanup = AsyncMock()
    server = AsyncMock(return_value=runner)
    engine = MagicMock()
    execution = MagicMock()
    broker = MagicMock()
    broker.close = AsyncMock()
    owner = MagicMock()
    build = MagicMock(return_value=(execution, broker, owner))
    operator_token = secrets.token_urlsafe(32)
    proposal_token = secrets.token_urlsafe(32)
    live = LiveSettings(
        enabled=mode != "disabled",
        api_token=operator_token,
        proposal_token=proposal_token,
        kalshi_key_id="fixture-key" if mode == "configured" else "",
        kalshi_private_key=SecretStr("fixture-only") if mode == "configured" else None,
    )
    monkeypatch.setattr(cli, "signer_from", lambda _: signer)
    monkeypatch.setattr(cli, "KalshiRest", MagicMock())
    monkeypatch.setattr(cli, "KalshiWebSocket", MagicMock())
    monkeypatch.setattr(cli, "Recorder", MagicMock(return_value=recorder))
    monkeypatch.setattr(cli, "attach_engine", MagicMock(return_value=engine))
    monkeypatch.setattr(cli, "LiveSettings", lambda: live)
    monkeypatch.setattr(cli, "build_service", build)
    monkeypatch.setattr(dashboard_http, "serve", server)
    await cli.record(Settings(data_dir=tmp_path, dashboard_token=SecretStr("dashboard")))
    runner.cleanup.assert_awaited_once()
    recorder.run.assert_awaited_once()
    arguments = server.call_args.args
    assert arguments[0] == tmp_path / "dashboard"
    assert arguments[1] == "dashboard"
    assert arguments[4:6] == (operator_token, proposal_token)
    if mode == "configured":
        assert arguments[3] is execution
        assert build.call_args.args == (
            tmp_path / "execution",
            live,
            engine,
            "fixture-key",
            b"fixture-only",
        )
        assert execution.run_reconciliation in recorder.background
        broker.close.assert_awaited_once()
        owner.close.assert_called_once()
    else:
        build.assert_not_called()
        assert arguments[3] is None
        assert (
            "credentials" in arguments[6]
            if mode == "missing_credentials"
            else ("disabled" in arguments[6])
        )


async def test_recorder_without_dashboard_does_not_expose_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signer: KalshiSigner
) -> None:
    recorder = MagicMock()
    recorder.run = AsyncMock()
    monkeypatch.setattr(cli, "signer_from", lambda _: signer)
    monkeypatch.setattr(cli, "KalshiRest", MagicMock())
    monkeypatch.setattr(cli, "KalshiWebSocket", MagicMock())
    monkeypatch.setattr(cli, "Recorder", MagicMock(return_value=recorder))
    server = AsyncMock()
    monkeypatch.setattr(dashboard_http, "serve", server)
    await cli.record(Settings(data_dir=tmp_path))
    recorder.run.assert_awaited_once()
    server.assert_not_awaited()
