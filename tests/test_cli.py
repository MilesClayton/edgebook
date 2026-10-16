from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import SecretStr

from edgebook.app import cli
from edgebook.config import Settings
from edgebook.venues.kalshi.models import Balance, RestMarket
from tests.conftest import market


def pem() -> bytes:
    return ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


class FakeRest:
    def __init__(self, *a: Any, **k: Any) -> None:
        self.signer = k.get("signer")

    async def markets(self, series: str) -> list[RestMarket]:
        return [RestMarket.model_validate(market("A"))] if series == "KXBTCD" else []

    async def balance(self) -> Balance:
        return Balance(balance=12345)

    async def aclose(self) -> None:
        return None


def test_signer_from_requires_credentials() -> None:
    with pytest.raises(SystemExit, match="EDGEBOOK_KALSHI_KEY_ID"):
        cli.signer_from(Settings())


def test_signer_from_pem_content_or_path(tmp_path: Path) -> None:
    key = pem()
    assert (
        cli.signer_from(
            Settings(kalshi_key_id="k", kalshi_private_key=SecretStr(key.decode()))
        ).key_id
        == "k"
    )
    path = tmp_path / "k.pem"
    path.write_bytes(key)
    assert cli.signer_from(Settings(kalshi_key_id="k", kalshi_private_key_path=path)).key_id == "k"


def test_markets_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "KalshiRest", FakeRest)
    assert cli.main(["markets"]) == 0
    out = capsys.readouterr().out
    assert "KXBTCD: 1 open markets" in out
    assert "KXBTC15M: 0 open markets, next close -" in out


def test_balance_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "KalshiRest", FakeRest)
    monkeypatch.setenv("EDGEBOOK_KALSHI_KEY_ID", "k")
    monkeypatch.setenv("EDGEBOOK_KALSHI_PRIVATE_KEY", pem().decode())
    assert cli.main(["balance"]) == 0
    assert "paper: balance $123.45" in capsys.readouterr().out


def test_record_wires_production_market_data(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, Any] = {}

    class FakeRecorder:
        def __init__(self, **kw: Any) -> None:
            built.update(kw)

        async def run(self) -> None:
            return None

    monkeypatch.setattr(cli, "Recorder", FakeRecorder)
    monkeypatch.setenv("EDGEBOOK_KALSHI_KEY_ID", "k")
    monkeypatch.setenv("EDGEBOOK_KALSHI_PRIVATE_KEY", pem().decode())
    assert cli.main(["record"]) == 0
    assert built["series"] == ("KXBTCD", "KXBTC", "KXBTC15M")
    assert "demo" not in built["ws"]._url
