"""Runtime settings and the production trading guard.

The same code runs in both environments; ``EDGEBOOK_ENV`` picks the venue
endpoints. Live trading against production needs three independent things to
be true at once (see ``assert_live_trading_allowed``).
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

KALSHI_URLS = {
    "paper": "https://external-api.demo.kalshi.co/trade-api/v2",
    "production": "https://external-api.kalshi.com/trade-api/v2",
}
KALSHI_PROD_URL = KALSHI_URLS["production"]


class Env(StrEnum):
    PAPER = "paper"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EDGEBOOK_", frozen=True, extra="forbid")

    env: Env = Env.PAPER
    live_trading_enabled: bool = False
    viability_path: Path = Path("viability.json")
    min_viability_trades: int = Field(default=500, gt=0)

    # Kalshi credentials: PEM content (Fly/GitHub secret) or a path to the .pem file.
    kalshi_key_id: str | None = None
    kalshi_private_key: SecretStr | None = None
    kalshi_private_key_path: Path | None = None

    # Recorder
    data_dir: Path = Path("data")
    record_series: tuple[str, ...] = ("KXBTCD", "KXBTC", "KXBTC15M")
    record_indices: tuple[str, ...] = ("BRTI",)

    # Research
    history_dir: Path = Path("history")
    reports_dir: Path = Path("reports")
    calibration_series: tuple[str, ...] = ("KXBTCD",)

    def kalshi_pem(self) -> bytes | None:
        if self.kalshi_private_key is not None:
            return self.kalshi_private_key.get_secret_value().encode()
        if self.kalshi_private_key_path is not None:
            return self.kalshi_private_key_path.expanduser().read_bytes()
        return None

    @property
    def kalshi_trade_url(self) -> str:
        """Order-entry endpoint for this environment."""
        return KALSHI_URLS[self.env.value]

    @property
    def kalshi_market_data_url(self) -> str:
        """Market data always comes from production; the demo book is not realistic."""
        return KALSHI_PROD_URL


class Viability(BaseModel):
    """Paper-trading result written by the ``report`` command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_trades: int = Field(ge=0)
    mean_pnl_per_trade: float
    ci95_low: float
    ci95_high: float


class ProductionGuardError(RuntimeError):
    pass


def load_viability(path: Path) -> Viability | None:
    if not path.is_file():
        return None
    return Viability.model_validate(json.loads(path.read_text()))


def assert_live_trading_allowed(settings: Settings) -> None:
    """Raise unless production order entry is explicitly and provably allowed."""
    if settings.env is not Env.PRODUCTION:
        return
    if not settings.live_trading_enabled:
        raise ProductionGuardError("EDGEBOOK_LIVE_TRADING_ENABLED is not true")
    viability = load_viability(settings.viability_path)
    if viability is None:
        raise ProductionGuardError(f"no viability report at {settings.viability_path}")
    if viability.n_trades < settings.min_viability_trades:
        raise ProductionGuardError(
            f"only {viability.n_trades} paper trades, need {settings.min_viability_trades}"
        )
    if viability.ci95_low <= 0:
        raise ProductionGuardError(
            f"paper edge not proven: 95% CI low is {viability.ci95_low:.5f} per trade"
        )
