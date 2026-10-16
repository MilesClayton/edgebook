import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from edgebook.app import cli
from edgebook.config import ProductionGuardError, Settings
from edgebook.live import signals as live
from edgebook.live.snapshot import SnapshotBuilder
from edgebook.venues.kalshi.models import CreateOrderV2Response, RestMarket
from tests.conftest import market

NOW = datetime.now(UTC).replace(second=0, microsecond=0)


def seed_history(directory: Path, days: int = 12) -> None:
    from edgebook.history import coinbase

    rng = np.random.default_rng(0)
    n = days * 1440
    ts = int(NOW.timestamp()) - 60 * n + 60 * np.arange(1, n + 1, dtype=np.int64)
    close = 80_000 * np.exp(np.cumsum(rng.normal(0, 4e-4, n)))
    import pyarrow as pa
    import pyarrow.parquet as pq

    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"ts": ts, "close": close}, schema=coinbase.SCHEMA),
        coinbase.path_for(directory, "BTC-USD"),
    )


@pytest.fixture
def pricer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> live.LivePricer:
    seed_history(tmp_path)

    async def no_backfill(directory: Path, days: int) -> int:
        return 0

    monkeypatch.setattr("edgebook.live.signals.coinbase.backfill", no_backfill)
    return live.LivePricer(tmp_path)


def open_market(ticker: str, strike: float, bid: str, ask: str, minutes: float = 10) -> RestMarket:
    return RestMarket.model_validate(
        market(
            ticker,
            floor_strike=strike,
            strike_type="greater_or_equal",
            close_time=(NOW + timedelta(minutes=minutes)).isoformat(),
            yes_bid_dollars=bid,
            yes_ask_dollars=ask,
        )
    )


async def test_pricer_edges_and_filters(pricer: live.LivePricer) -> None:
    _, spot = await pricer.refresh_spot()
    deep_itm = pricer.price(open_market("A", spot * 0.98, "0.50", "0.52"), "KXBTC15M", spot, NOW)
    assert deep_itm is not None
    assert deep_itm.fair > 0.95
    assert deep_itm.best_side == "yes"
    assert deep_itm.best_edge == pytest.approx(deep_itm.fair - 0.52 - 0.02, abs=1e-9)
    assert pricer.price(open_market("B", spot, "0.50", "0.50"), "S", spot, NOW) is None  # crossed
    assert pricer.price(open_market("C", spot, "0.50", "0.52", minutes=0.5), "S", spot, NOW) is None
    with pytest.raises(RuntimeError, match="refresh_spot"):
        live.LivePricer(Path("/nonexistent")).price(open_market("D", 1, "0.1", "0.2"), "S", 1, NOW)


class FakeRest:
    def __init__(self, markets: list[RestMarket]) -> None:
        self._markets = markets
        self.orders: list[Any] = []

    async def markets(self, series: str, status: str | None = "open") -> list[RestMarket]:
        return self._markets

    async def create_order(self, order: Any) -> CreateOrderV2Response:
        self.orders.append(order)
        return CreateOrderV2Response(
            order_id="o1",
            fill_count=Decimal(1),
            remaining_count=Decimal(0),
            average_fill_price=Decimal(order.price),
            average_fee_paid=Decimal("0.02"),
            ts_ms=1,
        )

    async def aclose(self) -> None:
        return None


async def test_signals_and_snapshot(pricer: live.LivePricer, tmp_path: Path) -> None:
    _, spot = await pricer.refresh_spot()
    rest = FakeRest(
        [
            open_market("A", spot, "0.45", "0.47"),
            open_market("LATER", spot, "0.45", "0.47", minutes=70),
        ]
    )
    sigs = await live.signals(rest, pricer, "KXBTC15M")  # type: ignore[arg-type]
    assert [s.ticker for s in sigs] == ["A"]  # only the soonest-closing event
    assert await live.signals(FakeRest([]), pricer, "X") == []  # type: ignore[arg-type]
    snap = await SnapshotBuilder(rest, pricer, recorder=None, data_dir=tmp_path).build()  # type: ignore[arg-type]
    assert snap["spot"] == pytest.approx(spot)
    assert len(snap["spot_series"]) == 240
    assert set(snap["signals"]) == {"KXBTC15M", "KXBTCD"}
    assert snap["recorder"] == {}


def args(**kw: Any) -> argparse.Namespace:
    base = {
        "series": "KXBTC15M",
        "execute": False,
        "min_edge": 0.0,
        "confirm_real_money": False,
        "session_cap": None,
    }
    return argparse.Namespace(**{**base, **kw})


async def test_trade_once_dry_run_guards_and_execute(
    pricer: live.LivePricer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, spot = await pricer.refresh_spot()
    rest = FakeRest([open_market("A", spot * 0.98, "0.50", "0.52")])
    monkeypatch.setattr(cli, "KalshiRest", lambda *a, **k: rest)
    monkeypatch.setattr(cli, "LivePricer", lambda d: pricer)
    monkeypatch.setattr(cli, "signer_from", lambda s: None)
    settings = Settings(history_dir=tmp_path, reports_dir=tmp_path / "r")

    await cli.trade_once(settings, args())
    assert "dry run" in capsys.readouterr().out
    assert rest.orders == []

    with pytest.raises(SystemExit, match="below --min-edge"):
        await cli.trade_once(settings, args(execute=True, min_edge=0.99))
    prod = Settings(env="production", history_dir=tmp_path, reports_dir=tmp_path / "r")  # type: ignore[arg-type]
    with pytest.raises(SystemExit, match="confirm-real-money"):
        await cli.trade_once(prod, args(execute=True))
    assert rest.orders == []

    with pytest.raises(ProductionGuardError, match="LIVE_TRADING_ENABLED"):
        await cli.trade_once(prod, args(execute=True, confirm_real_money=True))
    assert rest.orders == []

    await cli.trade_once(settings, args(execute=True))
    order = rest.orders[0]
    assert (order.side, order.count, order.price, order.time_in_force) == (
        "bid",
        "1.00",
        "0.5200",
        "immediate_or_cancel",
    )
    logged = json.loads((tmp_path / "r" / "trades.jsonl").read_text().splitlines()[0])
    assert logged["manual_one_off"] is True
    assert logged["response"]["order_id"] == "o1"
    assert logged["session_cap_dollars"] == 5.0
    assert logged["reserved_dollars"] > 0


async def test_trade_once_no_market(
    pricer: live.LivePricer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "KalshiRest", lambda *a, **k: FakeRest([]))
    monkeypatch.setattr(cli, "LivePricer", lambda d: pricer)
    with pytest.raises(SystemExit, match="no priceable"):
        await cli.trade_once(Settings(history_dir=tmp_path), args())
