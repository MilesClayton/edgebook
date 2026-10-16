"""Edge simulators on hand-built markets where the right answer is obvious."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from edgebook.history import kalshi as kh
from edgebook.research import edges, flow, paths
from edgebook.research.calibrate import Spot

T0 = 1_790_000_000 // 900 * 900


def mkt(
    tk: str, open_ts: int, close_ts: int, result: str, floor: float = 100.0, **kw: Any
) -> dict[str, Any]:
    return {
        "ticker": tk,
        "event_ticker": kw.pop("event", "E"),
        "open_ts": open_ts,
        "close_ts": close_ts,
        "strike_type": kw.pop("st", "greater_or_equal"),
        "floor": floor,
        "cap": None,
        "result": result,
        **kw,
    }


def path(tk: str, start: int, quotes: list[tuple[float, float]]) -> list[dict[str, Any]]:
    return [
        {
            "ticker": tk,
            "end_ts": start + 60 * (i + 1),
            "bid": b,
            "ask": a,
            "last": None,
            "volume": 1.0,
        }
        for i, (b, a) in enumerate(quotes)
    ]


def flat_spot(n: int = 60 * 24 * 2, jump_at: int | None = None, jump: float = 0.0) -> Spot:
    ts = T0 - 86_400 + 60 * np.arange(n, dtype=np.int64)
    close = np.full(n, 100.0)
    if jump_at is not None:
        close[ts >= jump_at] *= 1 + jump
    return Spot(ts, close)


def test_lag_round_trip_take_profit_and_settle() -> None:
    m = mkt("A", T0, T0 + 900, "yes")
    quotes = [(0.40, 0.42)] * 3 + [(0.60, 0.62)] * 12  # quote catches up after the jump
    spot = flat_spot(jump_at=T0 + 180, jump=0.003)
    p = paths.LagParams(
        window_min=1,
        min_move_pct=0.08,
        max_mid_follow=0.02,
        take_profit=0.15,
        stop_loss=0.25,
        max_hold_min=5,
    )
    trades = paths.simulate_lag([m], {"A": path("A", T0, quotes)}, spot, p)
    assert trades[0].side == "yes"
    assert trades[0].reason == "tp"
    assert trades[0].pnl == pytest.approx(0.60 - paths.fee(0.60) - 0.42 - paths.fee(0.42))
    res = paths.search(
        [m], {"A": path("A", T0, quotes)}, spot, holdout_start=T0 + 10**6, params=[p]
    )
    assert res[0].train.n == 1
    assert "lag round-trips" in paths.render("S", res)
    assert len(paths.grid()) == 72
    # stop loss: quote falls instead
    down = [(0.40, 0.42)] * 3 + [(0.20, 0.22)] * 12
    sl = paths.simulate_lag([m], {"A": path("A", T0, down)}, spot, p)
    assert sl[0].reason == "sl"


def test_ladder_arb_finds_only_real_boxes() -> None:
    lo = mkt("K1", T0, T0 + 3600, "yes", floor=100, st="greater")
    hi = mkt("K2", T0, T0 + 3600, "no", floor=110, st="greater")
    # YES(K1) ask 0.40 + NO(K2) ask (1 - 0.65) = 0.75 + fees < 1: a box
    cs = {"K1": path("K1", T0, [(0.38, 0.40)]), "K2": path("K2", T0, [(0.65, 0.67)])}
    trades = edges.ladder_arb([lo, hi], cs)
    assert len(trades) == 1
    assert trades[0].pnl == pytest.approx(2 - trades[0].entry)  # both legs paid out
    fair = {"K1": path("K1", T0, [(0.60, 0.62)]), "K2": path("K2", T0, [(0.30, 0.32)])}
    assert edges.ladder_arb([lo, hi], fair) == []


def test_early_leader_and_expiry_decay_and_runner() -> None:
    m = mkt("A", T0, T0 + 900, "yes")
    quotes = [(0.50, 0.52), (0.55, 0.57), (0.56, 0.58)] + [(0.99, 1.0)] * 11
    cs = {"A": path("A", T0, quotes)}
    el = edges.early_leader([m], cs)
    assert el[0].side == "yes"
    assert el[0].pnl == pytest.approx(0.99 - paths.fee(0.99) - 0.57 - paths.fee(0.57))

    spot = flat_spot()
    deep = mkt("B", T0, T0 + 900, "yes", floor=99.0)  # spot 100 vs strike 99: certain YES
    cs_b = {"B": path("B", T0, [(0.60, 0.62)] * 14)}
    noisy = flat_spot()
    noisy.close = noisy.close * (1 + 1e-5 * np.sin(np.arange(noisy.close.size)))
    ed = edges.expiry_decay([deep], cs_b, noisy)
    assert ed
    assert ed[0].side == "yes"
    assert edges.reclaim([m], cs, spot) == []

    res = edges.run_all({"KXBTCD": ([], {}), "KXBTC15M": ([m], cs)}, spot, holdout_start=T0)
    text = edges.render(res)
    assert "early_leader" in text
    assert "ladder_arb" in text


def test_flow_maker_taker_accounting(tmp_path: Path) -> None:
    markets = [mkt("A", T0, T0 + 900, "yes"), mkt("B", T0, T0 + 900, "")]
    trades = [
        {"ticker": "A", "ts": T0 + 100, "yes_price": 0.40, "count": 10.0, "taker_side": "yes"},
        {"ticker": "A", "ts": T0 + 890, "yes_price": 0.90, "count": 2.0, "taker_side": "no"},
        {
            "ticker": "B",
            "ts": T0 + 100,
            "yes_price": 0.50,
            "count": 5.0,
            "taker_side": "yes",
        },  # unsettled
    ]
    by_price = flow.analyze(trades, markets, "price")
    assert sum(b.trades for b in by_price) == 2
    b = next(x for x in by_price if x.label.startswith("taker pays 0.35"))
    assert b.taker.mean == pytest.approx(1 - 0.40 - 0.07 * 0.4 * 0.6)
    assert b.maker.mean == pytest.approx(0 - 0.60 - 0.0175 * 0.6 * 0.4)
    by_time = flow.analyze(trades, markets, "time")
    assert {x.label for x in by_time} == {"10-30 min left", "0-2 min left"}
    assert "maker $/ct" in flow.render("S", by_time)


async def test_flow_backfill_trades(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from edgebook.core.types import Side
    from edgebook.venues.kalshi.models import PublicTrade

    pq.write_table(
        pa.Table.from_pylist(
            [{**mkt("A", T0, T0 + 900, "yes"), "series": "S", "expiration_value": 1.0}],
            schema=kh.MARKETS,
        ),
        kh.markets_path(tmp_path, "S"),
    )

    class Rest:
        calls = 0

        async def market_trades(self, ticker: str) -> list[PublicTrade]:
            Rest.calls += 1
            return [
                PublicTrade(
                    trade_id="t",
                    ticker=ticker,
                    yes_price_dollars=Decimal("0.5"),
                    count_fp=Decimal(1),
                    taker_side=Side.YES,
                    created_time=datetime(2026, 1, 1, tzinfo=UTC),
                )
            ]

    assert await flow.backfill_trades(Rest(), tmp_path, "S") == 1  # type: ignore[arg-type]
    assert await flow.backfill_trades(Rest(), tmp_path, "S") == 0  # type: ignore[arg-type]
    assert Rest.calls == 1
