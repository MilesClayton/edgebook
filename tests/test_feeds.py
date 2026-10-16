"""Normalized live-spot parsers and composite calculations."""

import json

from edgebook.feeds.bitstamp import BitstampParser
from edgebook.feeds.coinbase import CoinbaseParser
from edgebook.feeds.composite import CompositeSpot
from edgebook.feeds.kraken import KrakenParser
from edgebook.feeds.types import BookQuote, TradeTick


def quote(venue: str, bid: float, ask: float, received: int = 1_000) -> BookQuote:
    return BookQuote(venue, "BTC/USD", bid, 2.0, ask, 3.0, received - 8, received)


def test_coinbase_snapshot_update_and_aggressor_side() -> None:
    parser = CoinbaseParser()
    snapshot = json.dumps(
        {
            "type": "snapshot",
            "product_id": "BTC-USD",
            "bids": [["100", "2"]],
            "asks": [["101", "3"]],
        }
    )
    event = parser.parse(snapshot, 1_000)[0]
    assert isinstance(event, BookQuote)
    assert (event.bid, event.ask) == (100, 101)
    update = json.dumps(
        {
            "type": "l2update",
            "product_id": "BTC-USD",
            "time": "2026-01-01T00:00:01Z",
            "changes": [["buy", "100", "0"], ["buy", "99", "4"]],
        }
    )
    event = parser.parse(update, 2_000)[0]
    assert isinstance(event, BookQuote)
    assert event.bid == 99
    trade = json.dumps(
        {"type": "match", "product_id": "BTC-USD", "side": "sell", "price": "101", "size": "0.5"}
    )
    tick = parser.parse(trade, 3_000)[0]
    assert isinstance(tick, TradeTick)
    assert tick.side == "buy"


def test_kraken_snapshot_update_and_trade() -> None:
    parser = KrakenParser()
    snapshot = json.dumps(
        {
            "channel": "book",
            "type": "snapshot",
            "data": [
                {
                    "symbol": "BTC/USD",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "bids": [{"price": 100, "qty": 2}],
                    "asks": [{"price": 101, "qty": 3}],
                }
            ],
        }
    )
    event = parser.parse(snapshot, 1_000)[0]
    assert isinstance(event, BookQuote)
    assert event.ask_size == 3
    update = json.dumps(
        {
            "channel": "book",
            "type": "update",
            "data": [
                {"symbol": "BTC/USD", "bids": [{"price": 100, "qty": 0}, {"price": 99, "qty": 4}]}
            ],
        }
    )
    event = parser.parse(update, 2_000)[0]
    assert isinstance(event, BookQuote)
    assert event.bid == 99
    trade = json.dumps(
        {
            "channel": "trade",
            "type": "update",
            "data": [{"symbol": "BTC/USD", "side": "sell", "price": 100, "qty": 0.4}],
        }
    )
    tick = parser.parse(trade, 3_000)[0]
    assert isinstance(tick, TradeTick)
    assert tick.side == "sell"


def test_bitstamp_book_and_trade() -> None:
    parser = BitstampParser()
    book = json.dumps(
        {
            "event": "data",
            "channel": "order_book_btcusd",
            "data": {
                "microtimestamp": "1000000",
                "bids": [["100", "2"]],
                "asks": [["101", "3"]],
            },
        }
    )
    event = parser.parse(book, 1_005)[0]
    assert isinstance(event, BookQuote)
    assert event.event_ts_ms == 1_000
    trade = json.dumps(
        {
            "event": "trade",
            "channel": "live_trades_btcusd",
            "data": {"microtimestamp": "2000000", "price": "101", "amount": "0.25", "type": 1},
        }
    )
    tick = parser.parse(trade, 2_005)[0]
    assert isinstance(tick, TradeTick)
    assert tick.side == "sell"


def test_composite_excludes_stale_quotes_and_finishes_second_bar() -> None:
    composite = CompositeSpot(max_stale_ms=500, flow_window_ms=10_000)
    composite.ingest(quote("coinbase", 100, 102, 1_000))
    second = composite.ingest(quote("kraken", 102, 104, 1_100))
    assert second.snapshot.mid == 102
    assert second.snapshot.cross_exchange_spread_bps == 2 / 102 * 10_000
    trade = composite.ingest(TradeTick("coinbase", "BTC/USD", "buy", 103, 2, 1_200, 1_200))
    assert trade.snapshot.trade_flow_imbalance == 1
    next_second = composite.ingest(quote("coinbase", 101, 103, 2_000))
    assert next_second.completed_bar is not None
    assert next_second.completed_bar.buy_volume == 2
    later = composite.snapshot(2_000)
    assert later.venues["kraken"].active is False
