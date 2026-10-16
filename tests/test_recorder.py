import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from edgebook.recorder.rows import message_rows
from edgebook.recorder.run import Recorder
from edgebook.recorder.sink import ParquetSink
from edgebook.venues.kalshi.models import WS_MESSAGE, DataMessage, RestMarket
from edgebook.venues.kalshi.ws import Subscription
from tests.conftest import market

T0 = 1_792_130_000_000  # 2026-10-16


def parse(payload: dict[str, Any]) -> DataMessage:
    msg = WS_MESSAGE.validate_python(payload)
    assert msg.type in {"orderbook_snapshot", "orderbook_delta", "trade", "cfbenchmarks_value"}
    return msg


SNAP = parse(
    {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": 1,
        "sending_ts_ms": T0,
        "msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.08", "300"]],
            "no_dollars_fp": [["0.5", "2"]],
        },
    }
)
DELTA = parse(
    {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": 2,
        "msg": {
            "market_ticker": "T",
            "price_dollars": "0.0800",
            "delta_fp": "-54.00",
            "side": "yes",
        },
    }
)
TRADE = parse(
    {
        "type": "trade",
        "sid": 2,
        "seq": 1,
        "msg": {
            "trade_id": "t1",
            "market_ticker": "T",
            "yes_price_dollars": "0.36",
            "count_fp": "136.00",
            "taker_side": "no",
            "ts_ms": T0,
        },
    }
)
INDEX = parse(
    {
        "type": "cfbenchmarks_value",
        "sid": 3,
        "seq": 1,
        "msg": {
            "index_id": "BRTI",
            "received_at": T0,
            "data": "{}",
            "avg_60s_data": {
                "value": "112345.12345678",
                "window_size": 60,
                "window_start_ts_ms": T0 - 60_000,
                "window_end_ts_exclusive": T0,
            },
        },
    }
)


def test_snapshot_rows_start_with_reset() -> None:
    rows = list(message_rows(SNAP, T0))
    assert [r["kind"] for _, r in rows] == ["reset", "level", "level"]
    assert rows[1][1]["price"] == Decimal("0.0800")
    assert rows[2][1]["side"] == "no"


def test_sink_roundtrip_all_streams(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path)
    for msg in (SNAP, DELTA, TRADE, INDEX):
        for stream, row in message_rows(msg, T0):
            sink.add(stream, row)
    sink.flush_all()
    sink.flush_all()  # nothing buffered: no empty files
    book = pq.read_table(tmp_path / "book").to_pylist()
    assert [r["kind"] for r in book] == ["reset", "level", "level", "delta"]
    assert book[3]["qty"] == Decimal("-54.00")
    assert pq.read_table(tmp_path / "trades").to_pylist()[0]["taker_side"] == "no"
    assert pq.read_table(tmp_path / "index").to_pylist()[0]["avg_60s"] == Decimal("112345.12345678")
    assert sink.rows_written["book"] == 4
    assert all(p.parent.name == "date=2026-10-16" for p in tmp_path.rglob("*.parquet"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_sink_flushes_on_size_and_age(tmp_path: Path) -> None:
    now = [0.0]
    sink = ParquetSink(tmp_path, max_rows=3, max_age_s=10, clock=lambda: now[0])
    for _, row in message_rows(SNAP, T0):
        sink.add("book", row)
    assert sink.rows_written["book"] == 3  # hit max_rows
    for _, row in message_rows(TRADE, T0):
        sink.add("trades", row)
    sink.flush_due()
    assert sink.rows_written["trades"] == 0
    now[0] = 11
    sink.flush_due()
    assert sink.rows_written["trades"] == 1


class FakeRest:
    def __init__(self, open_: list[dict[str, Any]], settled: list[dict[str, Any]]) -> None:
        self.open = [RestMarket.model_validate(m) for m in open_]
        self.settled = [RestMarket.model_validate(m) for m in settled]
        self.closed = False

    async def markets(
        self, series: str, status: str | None = "open", min_close: datetime | None = None
    ) -> list[RestMarket]:
        return self.open if status == "open" else self.settled

    async def aclose(self) -> None:
        self.closed = True


class FakeWs:
    def __init__(self, msgs: list[DataMessage]) -> None:
        self.msgs = msgs
        self.subs: list[Subscription] = []
        self.market_adds: list[list[str]] = []
        self.reconnects = 0
        self.gaps = 0

    def add(self, sub: Subscription) -> None:
        self.subs.append(sub)

    def add_markets(self, channels: Any, tickers: list[str]) -> None:
        self.market_adds.append(list(tickers))

    async def messages(self) -> AsyncIterator[DataMessage]:
        for m in self.msgs:
            yield m

    async def close(self) -> None:
        return None


def recorder(tmp_path: Path, rest: FakeRest, ws: FakeWs) -> Recorder:
    return Recorder(rest, ws, ParquetSink(tmp_path), ["KXBTCD"], ["BRTI"])  # type: ignore[arg-type]


async def test_discover_subscribes_only_new_markets(tmp_path: Path) -> None:
    ws = FakeWs([])
    rec = recorder(tmp_path, FakeRest([market("A"), market("B")], []), ws)
    assert await rec.discover_once() == ["A", "B"]
    assert await rec.discover_once() == []
    assert ws.market_adds == [["A", "B"]]


async def test_settle_records_each_result_once(tmp_path: Path) -> None:
    settled = [
        market("A", status="finalized", result="yes", expiration_value="90400.10"),
        market("B"),
    ]
    rec = recorder(tmp_path, FakeRest([], settled), FakeWs([]))
    assert await rec.settle_once() == 1
    assert await rec.settle_once() == 0
    rec.sink.flush_all()
    rows = pq.read_table(tmp_path / "settlements").to_pylist()
    assert rows[0]["result"] == "yes"
    assert rows[0]["floor_strike"] == Decimal("90299.99")


async def test_run_streams_to_disk_and_cleans_up(tmp_path: Path) -> None:
    rest = FakeRest([market("A")], [])
    ws = FakeWs([SNAP, DELTA, TRADE, INDEX])
    rec = recorder(tmp_path, rest, ws)
    run = asyncio.create_task(rec.run())
    async with asyncio.timeout(5):
        while rec.messages < 4 or not rec.subscribed:
            await asyncio.sleep(0.01)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert rest.closed
    assert ws.subs[0].index_ids == ("BRTI",)
    assert pq.read_table(tmp_path / "book").num_rows == 4
    assert pq.read_table(tmp_path / "markets").num_rows == 1
