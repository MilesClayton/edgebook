"""Tick replay on a small synthetic recorder dataset."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from edgebook.recorder.sink import ParquetSink
from edgebook.research import tick
from edgebook.research.tick_strategies import (
    AvellanedaStoikov,
    LagStrategy,
    PairedBids,
    SettlementWindow,
)

T0 = 1_792_130_400  # a 15-minute boundary
TK = "KXBTC15M-TEST"
CLOSE = T0 + 900
STRIKE = 82_000.0


def book(
    ts_ms: int, kind: str, side: str | None = None, price: str | None = None, qty: str | None = None
) -> dict[str, Any]:
    return {
        "recv_ts_ms": ts_ms,
        "sending_ts_ms": None,
        "sid": 1,
        "seq": 1,
        "kind": kind,
        "ticker": TK,
        "side": side,
        "price": Decimal(price) if price else None,
        "qty": Decimal(qty) if qty else None,
    }


def write_dataset(
    d: Path, result: str = "yes", jump: float = 0.0, settle_price: float | None = None
) -> None:
    sink = ParquetSink(d)
    sink.add(
        "markets",
        {
            "seen_ts_ms": T0 * 1000,
            "series": "KXBTC15M",
            "ticker": TK,
            "event_ticker": "E",
            "status": "active",
            "close_time": datetime.fromtimestamp(CLOSE, tz=UTC),
            "strike_type": "greater_or_equal",
            "floor_strike": Decimal("82000.00"),
            "cap_strike": None,
            "result": "",
            "expiration_value": "",
        },
    )
    sink.add(
        "settlements",
        {
            "seen_ts_ms": CLOSE * 1000,
            "series": "KXBTC15M",
            "ticker": TK,
            "event_ticker": "E",
            "status": "finalized",
            "close_time": datetime.fromtimestamp(CLOSE, tz=UTC),
            "strike_type": "greater_or_equal",
            "floor_strike": Decimal("82000.00"),
            "cap_strike": None,
            "result": result,
            "expiration_value": "82100",
        },
    )
    start = (T0 + 10) * 1000
    for row in (
        book(start, "reset"),
        book(start, "level", "yes", "0.4500", "50"),
        book(start, "level", "no", "0.5300", "50"),
    ):
        sink.add("book", row)
    for s in range(0, 890):
        ts = (T0 + 11 + s) * 1000
        v = STRIKE * (1 + (jump if s >= 600 else 0.0)) * (1 + 1e-5 * ((s % 7) - 3))
        if settle_price is not None and s >= 830:
            v = settle_price
        sink.add(
            "index",
            {
                "recv_ts_ms": ts,
                "received_at_ms": ts,
                "index_id": "BRTI",
                "avg_60s": Decimal(f"{v:.8f}"),
                "window_start_ms": ts - 60_000,
                "window_end_ms": ts,
                "raw": json.dumps({"value": f"{v:.2f}"}),
            },
        )
        if s % 10 == 0:  # book activity so quoting strategies get callbacks
            sink.add("book", book(ts + 1, "delta", "yes", "0.4500", "1"))
            sink.add("book", book(ts + 2, "delta", "yes", "0.4500", "-1"))
        if s % 15 == 0:  # sellers hit YES bids; buyers lift YES asks
            sink.add(
                "trades",
                {
                    "recv_ts_ms": ts + 3,
                    "ts_ms": ts,
                    "trade_id": f"a{s}",
                    "ticker": TK,
                    "yes_price": Decimal("0.4500"),
                    "count": Decimal("30"),
                    "taker_side": "no",
                },
            )
            sink.add(
                "trades",
                {
                    "recv_ts_ms": ts + 4,
                    "ts_ms": ts,
                    "trade_id": f"b{s}",
                    "ticker": TK,
                    "yes_price": Decimal("0.4700"),
                    "count": Decimal("30"),
                    "taker_side": "yes",
                },
            )
    sink.flush_all()


class Probe:
    """Exercises the Context API directly."""

    name = "probe"

    def __init__(self) -> None:
        self.done = False
        self.held: list[tick.Fill] = []

    def on_index(self, ctx: tick.Context) -> None:
        if self.done or ctx.markets.get(TK) is None or TK not in ctx.books:
            return
        assert ctx.best(TK) == (0.45, 50.0, 0.47, 50.0)
        assert ctx.index_at(10**6) is None
        yes = ctx.take(TK, "yes", "taker_yes")
        no = ctx.take(TK, "no", "taker_no")
        assert yes is not None
        assert yes.price == pytest.approx(0.47)
        assert no is not None
        assert no.price == pytest.approx(0.55)
        assert ctx.sell(no, "exit")
        ctx.rest(TK, "yes", 0.45, ttl_s=600, tag="maker")
        ctx.rest(TK, "no", 0.53, ttl_s=1, tag="expires")
        self.done = True

    def on_book(self, ctx: tick.Context, ticker: str) -> None:
        assert ctx.best("UNKNOWN") == (None, 0.0, None, 0.0)


def test_context_fills_queue_and_settlement(tmp_path: Path) -> None:
    write_dataset(tmp_path, result="yes")
    r = tick.replay(tmp_path, Probe())
    assert r.maker_fills == 1  # 50 ahead in queue, 30 per print: fills on the third print
    tags = r.by_tag
    assert tags["taker_yes"].mean == pytest.approx(1 - 0.47 - 0.0175)
    assert tags["maker"].mean == pytest.approx(1 - 0.45)
    assert tags["taker_no"].mean == pytest.approx(0.53 - 0.0175 - 0.55 - 0.0174)  # sold at NO bid
    assert "expires" not in tags
    assert r.open_positions == 0


def test_strategies_run_end_to_end(tmp_path: Path) -> None:
    write_dataset(tmp_path, result="yes", jump=0.002)
    lag = tick.replay(tmp_path, LagStrategy(window_s=15, min_move_pct=0.08))
    assert lag.trades >= 1
    assert lag.by_tag["lag"].mean > 0  # jumped up, bought YES at 47c, settled YES
    hold = tick.replay(
        tmp_path, LagStrategy(name="lag_hold", window_s=15, min_move_pct=0.08, hold_s=5)
    )
    assert hold.settled >= 1
    maker = tick.replay(
        tmp_path, LagStrategy(name="lag_maker", window_s=15, min_move_pct=0.08, maker=True)
    )
    assert maker.trades == maker.maker_fills
    for strat in (PairedBids(max_pair=0.99), AvellanedaStoikov()):  # synthetic bids sum to 98c
        res = tick.replay(tmp_path, strat)
        assert res.trades == res.maker_fills > 0


def test_settlement_window_uses_known_average(tmp_path: Path) -> None:
    write_dataset(tmp_path, result="yes", settle_price=STRIKE * 1.003)
    r = tick.replay(tmp_path, SettlementWindow(min_edge=0.03))
    assert r.trades == 1
    assert r.by_tag["settle_window"].mean > 0
