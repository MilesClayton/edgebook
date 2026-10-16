from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from edgebook.history import coinbase
from edgebook.history import kalshi as kh
from edgebook.venues.kalshi.models import RestMarket
from edgebook.venues.kalshi.ratelimit import TokenBucket
from edgebook.venues.kalshi.rest import KalshiRest
from tests.conftest import market

NOW = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)


def candles_handler(req: httpx.Request) -> httpx.Response:
    start = datetime.fromisoformat(req.url.params["start"])
    end = datetime.fromisoformat(req.url.params["end"])
    rows = []
    t = start
    while t < end:
        s = int(t.timestamp())
        rows.append([s, 1, 2, 1, 80_000 + (s // 60) % 7, 1.0])
        t += timedelta(minutes=1)
    rows.append([int(end.timestamp()) + 600, 1, 2, 1, 1.0, 1.0])  # outside window: ignored
    return httpx.Response(200, json=rows[::-1])


async def test_coinbase_backfill_is_incremental(tmp_path: Path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(candles_handler))
    first = await coinbase.backfill(tmp_path, days=1, client=client, now=NOW)
    assert first == 1440
    again = await coinbase.backfill(
        tmp_path, days=1, client=client, now=NOW + timedelta(minutes=10)
    )
    assert again == 10
    ts, close = coinbase.load(tmp_path)
    assert np.all(np.diff(ts) == 60)
    assert ts[-1] == int((NOW + timedelta(minutes=10)).timestamp())
    assert close.min() >= 80_000
    # A longer window later fills the older gap; a shorter one never deletes history.
    older = await coinbase.backfill(
        tmp_path, days=2, client=client, now=NOW + timedelta(minutes=10)
    )
    assert older == 1430  # 2-day window ends 10 min later than the first run
    assert (
        await coinbase.backfill(tmp_path, days=1, client=client, now=NOW + timedelta(minutes=10))
        == 0
    )
    ts, _ = coinbase.load(tmp_path)
    assert ts.size == 2 * 1440
    assert np.all(np.diff(ts) == 60)


def crow(end: int, bid: float, ask: float) -> dict[str, Any]:
    return {"ticker": "A", "end_ts": end, "bid": bid, "ask": ask, "last": None, "volume": 0.0}


def test_quote_at_uses_last_candle_at_or_before() -> None:
    cs = [crow(100, 0.40, 0.42), crow(160, 0.45, 0.47)]
    assert kh.quote_at(cs, 159) == (0.40, 0.42)
    assert kh.quote_at(cs, 160) == (0.45, 0.47)
    assert kh.quote_at(cs, 99) is None
    assert kh.quote_at(cs, 160 + 301) is None


def test_tradeable_filter() -> None:
    assert kh.tradeable(0.30, 0.33)
    assert not kh.tradeable(None, 0.33)
    assert not kh.tradeable(0.33, 0.33)
    assert not kh.tradeable(0.0, 0.02)  # mid below 3c
    assert not kh.tradeable(0.30, 0.45)  # spread over 10c


async def test_kalshi_backfill_keeps_tradeable_markets_and_skips_done_events(
    tmp_path: Path,
) -> None:
    close = int(NOW.timestamp())
    calls: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path.endswith("/markets"):
            settled: list[dict[str, Any]] = [
                market("A", result="yes", expiration_value="90400.10"),
                market(
                    "B",
                    result="no",
                    expiration_value="90400.10",
                    floor_strike=None,
                    cap_strike=91000,
                    strike_type="less",
                ),
                market("D", result="no"),  # never tradeable: dropped
                market("C", result=""),  # unsettled: skipped
            ]
            for m in settled:
                m["close_time"] = NOW.isoformat()
                m["open_time"] = (NOW - timedelta(hours=1)).isoformat()
            return httpx.Response(200, json={"markets": settled})
        tickers = req.url.params["market_tickers"].split(",")
        return httpx.Response(
            200,
            json={
                "markets": [
                    {
                        "market_ticker": t,
                        "candlesticks": [
                            {
                                "end_period_ts": close - 60 * m,
                                "yes_bid": {"close_dollars": "0.00" if t == "D" else "0.30"},
                                "yes_ask": {"close_dollars": "0.01" if t == "D" else "0.33"},
                                "price": {"close_dollars": None},
                                "volume_fp": "5.00",
                            }
                            for m in (60, 30, 15, 5)
                        ],
                    }
                    for t in tickers
                ]
            },
        )

    rest = KalshiRest(
        "https://x.test/v2",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        bucket=TokenBucket(1e9, capacity=1e9),
    )
    assert await kh.backfill(rest, tmp_path, "KXBTCD", days=1, now=NOW + timedelta(hours=1)) == 2
    markets = kh.load_markets(tmp_path, "KXBTCD")
    candles = kh.load_candles(tmp_path, "KXBTCD")
    assert {m["ticker"] for m in markets} == {"A", "B"}
    assert len(candles["A"]) == 4
    rows = kh.horizon_rows(markets, candles)
    assert {(r["ticker"], r["horizon_min"]) for r in rows} == {
        (t, h) for t in "AB" for h in kh.HORIZONS_MIN
    }
    b = next(r for r in rows if r["ticker"] == "B")
    assert b["cap"] == 91000.0
    assert b["floor"] is None
    assert b["bid"] == 0.30
    assert b["expiration_value"] == 90400.10
    # Second run: event already stored, no candle requests.
    n_candle_calls = calls.count("/v2/markets/candlesticks")
    assert await kh.backfill(rest, tmp_path, "KXBTCD", days=1, now=NOW + timedelta(hours=1)) == 0
    assert calls.count("/v2/markets/candlesticks") == n_candle_calls


def test_horizon_rows_skip_before_open_and_missing_quotes() -> None:
    m = kh.market_row("KXBTC15M", RestMarket.model_validate(market("A", result="yes")))
    m["open_ts"] = m["close_ts"] - 15 * 60
    rows = kh.horizon_rows([m], {}, horizons=(60, 10))
    assert [r["horizon_min"] for r in rows] == [10]  # 60 min is before the market opened
    assert rows[0]["bid"] is None
    assert kh._float("abc") is None
