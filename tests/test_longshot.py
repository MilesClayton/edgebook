"""The long-shot seller: quoting rule, event cap, fills and the research helpers."""

import json
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from edgebook.core.types import LiquidityRole
from edgebook.engine.decide import Call, EngineConfig, evaluate, fee, fill_fee
from edgebook.engine.loop import (
    DEFAULT_STRATEGIES,
    LONGSHOT_EVERY_MS,
    LiveEngine,
    longshot_log_rows,
)
from edgebook.engine.maker import LongshotConfig, MakerBook, longshot_quote, quote_longshot
from edgebook.engine.paper import PaperAccount
from edgebook.engine.state import LiveState, MarketInfo
from edgebook.research import longshot as ls
from tests.test_engine import NOW, world

SPEC = next(s for s in DEFAULT_STRATEGIES if s.name == "longshot")
CLOSE_H = NOW // 1000 + 1800  # an hourly market 30 min from close
MAKER = LiquidityRole.MAKER


def call(
    up_ask: float | None, down_ask: float | None, fair: float | None, left: float = 1800
) -> Call:
    return Call(
        "H",
        "KXBTCD",
        "NO TRADE",
        "",
        left,
        82_000.0,
        82_000.0,
        0,
        fair_up=fair,
        up_price=up_ask,
        down_price=down_ask,
    )


def test_longshot_quote_rules() -> None:
    cfg = LongshotConfig()
    assert longshot_quote(call(0.04, 0.97, 0.02), cfg) == pytest.approx(("no", 0.96, 0.98))
    assert longshot_quote(call(0.97, 0.05, 0.96), cfg) == pytest.approx(("yes", 0.95, 0.96))
    assert longshot_quote(call(0.04, 0.97, 0.05), cfg) is None  # fair says it's not overpriced
    assert longshot_quote(call(0.30, 0.71, 0.29), cfg) is None  # not a long shot
    assert longshot_quote(call(0.04, 0.97, 0.02, left=100), cfg) is None  # too close to close
    assert longshot_quote(call(0.04, 0.97, None), cfg) == pytest.approx(("no", 0.96, 0.96))


def hourly_world() -> tuple[LiveState, MarketInfo]:
    s = world(spot=81_800.0, yes_bid="0.02", no_bid="0.96")  # UP ask 0.04: a long shot
    m = MarketInfo(
        "KXBTCD-26OCT1611-T81999.99", "KXBTCD", CLOSE_H - 3600, CLOSE_H, "greater", 81_999.99, None
    )
    s.markets = {m.ticker: m}
    s.books[m.ticker] = s.books.pop("M")
    return s, m


def test_quote_longshot_respects_event_room_and_holdings() -> None:
    s, m = hourly_world()
    c = evaluate(s, m, NOW, EngineConfig(model_weight=0.2))
    acct = PaperAccount()
    book = MakerBook()
    cfg = LongshotConfig()
    o = quote_longshot(book, s, acct, c, cfg, acct.cash, NOW, True, 15_000.0)
    assert o is not None
    assert (o.side, o.price, o.queue_ahead) == ("no", 0.96, 500.0)
    assert o.contracts == 5_000 // (0.96 + fee(0.96, MAKER))  # 1% of equity
    assert quote_longshot(book, s, acct, c, cfg, acct.cash, NOW, True, 15_000.0) is o  # kept
    room = 100 * (0.96 + fee(0.96, MAKER)) + 1e-6
    small = quote_longshot(MakerBook(), s, acct, c, cfg, acct.cash, NOW, True, room)
    assert small is not None
    assert small.contracts == 100  # event cap binds
    assert quote_longshot(MakerBook(), s, acct, c, cfg, acct.cash, NOW, True, 0.0) is None
    assert quote_longshot(book, s, acct, c, cfg, acct.cash, NOW, False, 1e9) is None
    assert book.orders == {}


def test_engine_longshot_seller_fills_and_settles(tmp_path: Path) -> None:
    s, m = hourly_world()
    eng = LiveEngine(s, tmp_path / "p.json", clock=lambda: NOW, strategies=[SPEC])
    [c] = eng.tick()
    assert c.action == "BID DOWN"
    assert "Selling the UP long shot at 0.04" in c.reason
    [order] = eng.live_payload()["longshot_orders"]  # shown on Kalshi pages by the extension
    assert (order["ticker"], order["side"], order["price"]) == (m.ticker, "no", 0.96)
    s.add_trade(m.ticker, 0.04, 800, "yes", NOW)  # YES buyers lift the long shot: queue, then us
    eng.tick()
    h = eng.account.holdings[m.ticker]
    assert (h.side, h.contracts) == ("no", 300)
    other = MarketInfo(
        "KXBTCD-26OCT1611-T82099.99", "KXBTCD", CLOSE_H - 3600, CLOSE_H, "greater", 82_099.99, None
    )
    s.markets[other.ticker] = other
    # only strikes with a long-shot price are fully evaluated (cheap top-of-book check first)
    assert other not in eng.live_markets(NOW, eng.runners[0])  # no quotes yet
    s.books[other.ticker] = s.books[m.ticker]  # same 4c UP long shot
    assert other in eng.live_markets(NOW, eng.runners[0])  # any strike, not just the nearest
    # the filled part plus the order still working on that strike: the whole $2.5k budget
    assert eng._event_exposure(eng.runners[0], other.ticker) == pytest.approx(2_500, rel=0.01)
    eng.clock = lambda: CLOSE_H * 1000 + 1
    eng.tick()
    assert eng.pending_settlement == {m.ticker}
    eng.settle(m.ticker, "no")
    assert eng.account.realized == pytest.approx(300 * (1 - 0.96) - fill_fee(0.96, 300, MAKER))
    assert eng.paper_payload()["risk"]["max_open_markets"] == 40


def test_engine_logs_every_longshot_firing_with_fair_value(tmp_path: Path) -> None:
    s, m = hourly_world()
    eng = LiveEngine(
        s, tmp_path / "p.json", clock=lambda: NOW, strategies=[SPEC], decision_log_dir=tmp_path
    )
    [c] = eng.tick()
    assert c.fair_up is not None
    [path] = (tmp_path / "longshot").glob("*.jsonl")
    [row] = [json.loads(x) for x in path.read_text().splitlines()]
    assert row["ticker"] == m.ticker
    assert (row["long_is_up"], row["long_ask"], row["fires"]) == (True, 0.04, True)
    assert row["fair_long"] == pytest.approx(c.fair_up, abs=1e-6)
    assert row["edge"] == pytest.approx(0.04 - c.fair_up, abs=1e-6)
    assert row["resting_price"] == 0.96
    assert (row["held_contracts"], row["data_ok"], row["model_weight"]) == (0.0, True, 0.2)
    r = eng.runners[0]
    r.calls = [
        call(0.97, 0.05, 0.94),  # DOWN long shot that fair value says is worth its price
        call(0.30, 0.71, 0.29),  # not a long shot
        call(0.04, 0.97, 0.02, left=100),  # too close to the close
        call(0.04, 0.97, None),  # no fair value
    ]
    [down] = longshot_log_rows(r, NOW)
    assert (down["long_is_up"], down["long_ask"], down["fires"]) == (False, 0.05, False)
    assert down["edge"] == pytest.approx(0.05 - 0.06)
    assert down["resting_price"] is None


def test_engine_longshot_says_why_not(tmp_path: Path) -> None:
    eng = LiveEngine(
        world(spot=82_000.0), tmp_path / "p.json", clock=lambda: NOW, strategies=[SPEC]
    )
    assert eng.tick() == []  # a 50/50 market is not a long shot: not even evaluated
    s, _ = hourly_world()
    picky = replace(SPEC, longshot=LongshotConfig(min_secs_left=3000))  # needs 50+ min left
    late = LiveEngine(s, tmp_path / "r.json", clock=lambda: NOW, strategies=[picky])
    [c] = late.tick()  # a long shot, but too close to the close (for this config) to sell
    assert c.action == "NO TRADE"
    assert c.reason.startswith("No long shot to sell")
    s2, _ = hourly_world()
    stale = LiveEngine(s2, tmp_path / "q.json", clock=lambda: NOW + 9_000, strategies=[SPEC])
    [c2] = stale.tick()
    assert c2.reason.startswith("Waiting")
    # re-quotes every LONGSHOT_EVERY_MS; in between it only matches fills
    stale.clock = lambda: NOW + 9_000 + LONGSHOT_EVERY_MS // 2
    stale.tick()
    assert stale.runners[0].last_tick_ms == NOW + 9_000  # skipped: last quotes stand
    stale.clock = lambda: NOW + 14_000
    stale.tick()
    assert stale.runners[0].last_tick_ms == NOW + 14_000


def test_longshot_research_tables(tmp_path: Path) -> None:
    markets = [
        {
            "series": "KXBTCD",
            "event_ticker": f"E{i % 5}",
            "ticker": f"T{i}",
            "open_ts": 0,
            "close_ts": 10_000 + i,
            "strike_type": "greater",
            "floor": 1.0,
            "cap": None,
            "result": "yes" if i % 25 == 0 else "no",
            "expiration_value": 1.0,
        }
        for i in range(100)
    ]
    candles = [
        {
            "ticker": f"T{i}",
            "end_ts": 10_000 + i - 1800,
            "bid": 0.03,
            "ask": 0.05,
            "last": 0.04,
            "volume": 1.0,
        }
        for i in range(100)
    ]
    pq.write_table(pa.Table.from_pylist(markets), tmp_path / "kalshi_markets_KXBTCD.parquet")
    pq.write_table(pa.Table.from_pylist(candles), tmp_path / "kalshi_candles_KXBTCD.parquet")
    qs = ls.quotes(tmp_path, "KXBTCD", horizons=(30, 5))
    assert len(qs) == 100  # no quote within 2 min of the 5-minute mark
    [row] = ls.bucket_report(qs, 30)
    assert row["yes_won"] == pytest.approx(0.04)
    assert row["buy_yes"] < 0  # long shots lose for their buyers
    lo, hi = row["buy_yes_ci"]
    assert lo <= row["buy_yes"] <= hi
    a, b = ls.split_in_time(qs)
    assert len(a) + len(b) == 100
