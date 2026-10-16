import json
from pathlib import Path

import pytest

from edgebook.config import (
    KALSHI_PROD_URL,
    Env,
    ProductionGuardError,
    Settings,
    assert_live_trading_allowed,
)


def write_viability(path: Path, n: int, ci_low: float) -> Path:
    path.write_text(
        json.dumps(
            {"n_trades": n, "mean_pnl_per_trade": 0.01, "ci95_low": ci_low, "ci95_high": 0.02}
        )
    )
    return path


def prod(tmp_path: Path, **kw: object) -> Settings:
    return Settings(env=Env.PRODUCTION, viability_path=tmp_path / "viability.json", **kw)  # type: ignore[arg-type]


def test_paper_is_default_and_uses_demo_for_orders() -> None:
    s = Settings()
    assert s.env is Env.PAPER
    assert "demo" in s.kalshi_trade_url
    assert s.kalshi_market_data_url == KALSHI_PROD_URL
    assert_live_trading_allowed(s)


def test_env_var_selects_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGEBOOK_ENV", "production")
    assert Settings().kalshi_trade_url == KALSHI_PROD_URL


def test_production_blocked_without_flag(tmp_path: Path) -> None:
    with pytest.raises(ProductionGuardError, match="LIVE_TRADING_ENABLED"):
        assert_live_trading_allowed(prod(tmp_path))


def test_production_blocked_without_report(tmp_path: Path) -> None:
    with pytest.raises(ProductionGuardError, match="no viability report"):
        assert_live_trading_allowed(prod(tmp_path, live_trading_enabled=True))


def test_production_blocked_with_too_few_trades(tmp_path: Path) -> None:
    write_viability(tmp_path / "viability.json", n=10, ci_low=0.01)
    with pytest.raises(ProductionGuardError, match="paper trades"):
        assert_live_trading_allowed(prod(tmp_path, live_trading_enabled=True))


def test_production_blocked_when_edge_unproven(tmp_path: Path) -> None:
    write_viability(tmp_path / "viability.json", n=1000, ci_low=-0.001)
    with pytest.raises(ProductionGuardError, match="not proven"):
        assert_live_trading_allowed(prod(tmp_path, live_trading_enabled=True))


def test_production_allowed_when_all_gates_pass(tmp_path: Path) -> None:
    write_viability(tmp_path / "viability.json", n=1000, ci_low=0.002)
    assert_live_trading_allowed(prod(tmp_path, live_trading_enabled=True))
