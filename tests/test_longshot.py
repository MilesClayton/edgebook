"""The long-shot seller: quoting rule, event cap, fills and the research helpers."""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from edgebook.core.types import LiquidityRole
from edgebook.engine.decide import Call, EngineConfig, evaluate, fee, fill_fee
from edgebook.engine.loop import DEFAULT_STRATEGIES, LiveEngine
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
    s.add_trade(m.ticker, 0.04, 800, "yes", NOW)  # YES buyers lift the long shot: queue, then us
    eng.tick()
    h = eng.account.holdings[m.ticker]
    assert (h.side, h.contracts) == ("no", 300)
    other = MarketInfo(
        "KXBTCD-26OCT1611-T82099.99", "KXBTCD", CLOSE_H - 3600, CLOSE_H, "greater", 82_099.99, None
    )
    s.markets[other.ticker] = other
    assert other in eng.live_markets(NOW, eng.runners[0])  # every strike, not just the nearest
    # the filled part plus the order still working on that strike: the whole $2.5k budget
    assert eng._event_exposure(eng.runners[0], other.ticker) == pytest.approx(2_500, rel=0.01)
    eng.clock = lambda: CLOSE_H * 1000 + 1
    eng.tick()
    assert eng.pending_settlement == {m.ticker}
    eng.settle(m.ticker, "no")
    assert eng.account.realized == pytest.approx(300 * (1 - 0.96) - fill_fee(0.96, 300, MAKER))
    assert eng.paper_payload()["risk"]["max_open_markets"] == 40


def test_engine_longshot_says_why_not(tmp_path: Path) -> None:
    eng = LiveEngine(
        world(spot=82_000.0), tmp_path / "p.json", clock=lambda: NOW, strategies=[SPEC]
    )
    [c] = [c for c in eng.tick() if c.ticker == "M"]
    assert c.action == "NO TRADE"
    assert c.reason.startswith("No long shot to sell")
    stale = LiveEngine(
        world(spot=82_000.0), tmp_path / "q.json", clock=lambda: NOW + 9_000, strategies=[SPEC]
    )
    [c2] = [c for c in stale.tick() if c.ticker == "M"]
    assert c2.reason.startswith("Waiting")


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
