"""Typed Kalshi wire models (REST + WebSocket), validated at the boundary.

Only the fields edgebook uses are modelled; extra fields are ignored so new
Kalshi fields don't break us, while a renamed/removed field fails loudly.
``tests/test_kalshi_spec_drift.py`` checks these names against Kalshi's
published OpenAPI/AsyncAPI specs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from edgebook.core.types import Market, Side


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# --- REST -------------------------------------------------------------------


class RestMarket(_Wire):
    ticker: str
    event_ticker: str
    status: str
    close_time: datetime
    strike_type: str | None = None
    floor_strike: Decimal | None = None
    cap_strike: Decimal | None = None
    result: str = ""
    expiration_value: str = ""

    def to_core(self, underlying: str) -> Market:
        return Market(
            venue="kalshi",
            ticker=self.ticker,
            event_ticker=self.event_ticker,
            underlying=underlying,
            strike=self.floor_strike if self.floor_strike is not None else self.cap_strike,
            close_time=self.close_time,
        )


class MarketsPage(_Wire):
    markets: list[RestMarket]
    cursor: str = ""


class Balance(_Wire):
    balance: int


# --- WebSocket --------------------------------------------------------------

Level = tuple[Decimal, Decimal]  # (price dollars, contract count)


class SnapshotMsg(_Wire):
    market_ticker: str
    yes_dollars_fp: list[Level] = Field(default_factory=list)
    no_dollars_fp: list[Level] = Field(default_factory=list)


class DeltaMsg(_Wire):
    market_ticker: str
    price_dollars: Decimal
    delta_fp: Decimal
    side: Side
    ts_ms: int | None = None


class TradeMsg(_Wire):
    trade_id: str
    market_ticker: str
    yes_price_dollars: Decimal
    count_fp: Decimal
    taker_side: Side
    ts_ms: int


class IndexAverage(_Wire):
    value: Decimal
    window_size: int
    window_start_ts_ms: int
    window_end_ts_exclusive: int


class IndexValueMsg(_Wire):
    index_id: str
    received_at: int
    data: str
    avg_60s_data: IndexAverage


class _Envelope(_Wire):
    sid: int
    seq: int
    sending_ts_ms: int | None = None


class OrderbookSnapshot(_Envelope):
    type: Literal["orderbook_snapshot"]
    msg: SnapshotMsg


class OrderbookDelta(_Envelope):
    type: Literal["orderbook_delta"]
    msg: DeltaMsg


class Trade(_Envelope):
    type: Literal["trade"]
    msg: TradeMsg


class IndexValue(_Envelope):
    type: Literal["cfbenchmarks_value"]
    msg: IndexValueMsg


class SubscribedMsg(_Wire):
    channel: str
    sid: int


class Subscribed(_Wire):
    type: Literal["subscribed"]
    id: int | None = None
    msg: SubscribedMsg


class ErrorMsg(_Wire):
    code: int
    msg: str


class WsError(_Wire):
    type: Literal["error"]
    id: int | None = None
    sid: int | None = None
    seq: int | None = None
    msg: ErrorMsg


class Ack(_Wire):
    """Command replies. Subscription-scoped ones consume a sequence number."""

    type: Literal["ok", "unsubscribed", "list_subscriptions"]
    id: int | None = None
    sid: int | None = None
    seq: int | None = None


WsMessage = Annotated[
    OrderbookSnapshot | OrderbookDelta | Trade | IndexValue | Subscribed | WsError | Ack,
    Field(discriminator="type"),
]
DataMessage = OrderbookSnapshot | OrderbookDelta | Trade | IndexValue
WS_MESSAGE: TypeAdapter[WsMessage] = TypeAdapter(WsMessage)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)
