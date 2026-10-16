"""Pure conversions from Kalshi wire messages to flat Parquet rows.

Schemas are explicit so every file in a stream has identical columns and the
backtest can read a day with one scan. Book snapshots start with a ``reset``
row so a replay knows to clear the ticker's book before applying levels.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pyarrow as pa

from edgebook.venues.kalshi.models import (
    DataMessage,
    IndexValue,
    OrderbookDelta,
    OrderbookSnapshot,
    RestMarket,
    Trade,
)

Row = dict[str, Any]

PRICE = pa.decimal128(8, 4)
QTY = pa.decimal128(18, 2)
INDEX = pa.decimal128(20, 8)
STRIKE = pa.decimal128(18, 2)


def make_schema(*cols: tuple[str, pa.DataType]) -> pa.Schema:
    return pa.schema([pa.field(name, dtype) for name, dtype in cols])


BOOK = make_schema(
    ("recv_ts_ms", pa.int64()),
    ("sending_ts_ms", pa.int64()),
    ("sid", pa.int64()),
    ("seq", pa.int64()),
    ("kind", pa.string()),  # reset | level | delta
    ("ticker", pa.string()),
    ("side", pa.string()),
    ("price", PRICE),
    ("qty", QTY),
)
TRADES = make_schema(
    ("recv_ts_ms", pa.int64()),
    ("ts_ms", pa.int64()),
    ("trade_id", pa.string()),
    ("ticker", pa.string()),
    ("yes_price", PRICE),
    ("count", QTY),
    ("taker_side", pa.string()),
)
INDEX_VALUES = make_schema(
    ("recv_ts_ms", pa.int64()),
    ("received_at_ms", pa.int64()),
    ("index_id", pa.string()),
    ("avg_60s", INDEX),
    ("window_start_ms", pa.int64()),
    ("window_end_ms", pa.int64()),
    ("raw", pa.string()),
)
MARKETS = make_schema(
    ("seen_ts_ms", pa.int64()),
    ("series", pa.string()),
    ("ticker", pa.string()),
    ("event_ticker", pa.string()),
    ("status", pa.string()),
    ("close_time", pa.timestamp("ms", tz="UTC")),
    ("strike_type", pa.string()),
    ("floor_strike", STRIKE),
    ("cap_strike", STRIKE),
    ("result", pa.string()),
    ("expiration_value", pa.string()),
)
SCHEMAS: dict[str, pa.Schema] = {
    "book": BOOK,
    "trades": TRADES,
    "index": INDEX_VALUES,
    "markets": MARKETS,
    "settlements": MARKETS,
}


def _q(value: Decimal | None, places: str) -> Decimal | None:
    return None if value is None else value.quantize(Decimal(places))


def message_rows(msg: DataMessage, recv_ts_ms: int) -> Iterator[tuple[str, Row]]:
    """Yield ``(stream, row)`` pairs for one data message."""
    env = {"recv_ts_ms": recv_ts_ms, "sending_ts_ms": msg.sending_ts_ms}
    if isinstance(msg, OrderbookSnapshot):
        base = {**env, "sid": msg.sid, "seq": msg.seq, "ticker": msg.msg.market_ticker}
        yield "book", {**base, "kind": "reset", "side": None, "price": None, "qty": None}
        for side, levels in (("yes", msg.msg.yes_dollars_fp), ("no", msg.msg.no_dollars_fp)):
            for price, qty in levels:
                yield (
                    "book",
                    {
                        **base,
                        "kind": "level",
                        "side": side,
                        "price": _q(price, "0.0001"),
                        "qty": _q(qty, "0.01"),
                    },
                )
    elif isinstance(msg, OrderbookDelta):
        yield (
            "book",
            {
                **env,
                "sid": msg.sid,
                "seq": msg.seq,
                "kind": "delta",
                "ticker": msg.msg.market_ticker,
                "side": msg.msg.side.value,
                "price": _q(msg.msg.price_dollars, "0.0001"),
                "qty": _q(msg.msg.delta_fp, "0.01"),
            },
        )
    elif isinstance(msg, Trade):
        t = msg.msg
        yield (
            "trades",
            {
                "recv_ts_ms": recv_ts_ms,
                "ts_ms": t.ts_ms,
                "trade_id": t.trade_id,
                "ticker": t.market_ticker,
                "yes_price": _q(t.yes_price_dollars, "0.0001"),
                "count": _q(t.count_fp, "0.01"),
                "taker_side": t.taker_side.value,
            },
        )
    elif isinstance(msg, IndexValue):
        v = msg.msg
        yield (
            "index",
            {
                "recv_ts_ms": recv_ts_ms,
                "received_at_ms": v.received_at,
                "index_id": v.index_id,
                "avg_60s": _q(v.avg_60s_data.value, "0.00000001"),
                "window_start_ms": v.avg_60s_data.window_start_ts_ms,
                "window_end_ms": v.avg_60s_data.window_end_ts_exclusive,
                "raw": v.data,
            },
        )


def market_row(series: str, market: RestMarket, seen_ts_ms: int) -> Row:
    return {
        "seen_ts_ms": seen_ts_ms,
        "series": series,
        "ticker": market.ticker,
        "event_ticker": market.event_ticker,
        "status": market.status,
        "close_time": market.close_time,
        "strike_type": market.strike_type,
        "floor_strike": _q(market.floor_strike, "0.01"),
        "cap_strike": _q(market.cap_strike, "0.01"),
        "result": market.result,
        "expiration_value": market.expiration_value,
    }
