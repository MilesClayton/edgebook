from pathlib import Path
from typing import Any

import numpy as np
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import SecretStr

from edgebook.app import cli
from edgebook.config import Settings
from edgebook.history import coinbase
from edgebook.history import kalshi as kalshi_history
from edgebook.research import calibrate as calib
from edgebook.research.calibrate import CalibrationReport
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


def test_backfill_and_calibrate_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, int]] = []

    async def cb_backfill(directory: Path, days: int) -> int:
        calls.append(("coinbase", days))
        return 1

    async def k_backfill(rest: Any, directory: Path, series: str, days: int) -> int:
        calls.append((series, days))
        return 2

    monkeypatch.setattr(coinbase, "backfill", cb_backfill)
    monkeypatch.setattr(kalshi_history, "backfill", k_backfill)
    monkeypatch.setenv("EDGEBOOK_HISTORY_DIR", str(tmp_path / "h"))
    monkeypatch.setenv("EDGEBOOK_REPORTS_DIR", str(tmp_path / "r"))
    assert cli.main(["backfill", "--days", "3"]) == 0
    assert calls == [("coinbase", 3 + calib.WARMUP_DAYS + 1), ("KXBTCD", 3)]

    monkeypatch.setattr(
        coinbase, "load", lambda d: (np.array([0], dtype=np.int64), np.array([1.0]))
    )
    monkeypatch.setattr(kalshi_history, "load", lambda d, s: [{"close_ts": 0}, {"close_ts": 2**40}])
    seen: list[int] = []

    def fake_run(ts: Any, close: Any, rows: list[dict[str, Any]], **kw: Any) -> CalibrationReport:
        seen.append(len(rows))
        kw["progress"](0, 1)
        return CalibrationReport(n_rows_in=len(rows), n_scored=0, skipped={}, basis_bps={})

    monkeypatch.setattr(calib, "run", fake_run)
    monkeypatch.setattr(calib, "render", lambda r: "REPORT")
    assert cli.main(["calibrate", "--days", "3"]) == 0
    assert seen == [1]  # the old row falls outside --days
    assert "REPORT" in capsys.readouterr().out
    assert len(list((tmp_path / "r").glob("calibration-*.json"))) == 1
