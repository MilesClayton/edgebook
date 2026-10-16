"""Live engine: state, calls, paper account, loop, all on synthetic data."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from edgebook.engine.decide import EngineConfig, Position, evaluate, fair_up, fee
from edgebook.engine.loop import LiveEngine
from edgebook.engine.paper import PaperAccount, RiskConfig
from edgebook.engine.state import Level, LiveState, MarketInfo
from edgebook.feeds.types import CompositeSnapshot
from edgebook.venues.kalshi.models import WS_MESSAGE, RestMarket
from tests.conftest import market

T0 = 1_792_130_400  # 15-minute boundary
CLOSE = T0 + 900
NOW = (T0 + 300) * 1000  # 10 minutes left


def msg(payload: dict[str, Any]) -> Any:
    return WS_MESSAGE.validate_python(payload)


def snapshot(ticker: str, yes: list[tuple[str, str]], no: list[tuple[str, str]]) -> Any:
    return msg(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {"market_ticker": ticker, "yes_dollars_fp": yes, "no_dollars_fp": no},
        }
    )


def index_tick(value: float) -> Any:
    return msg(
        {
            "type": "cfbenchmarks_value",
            "sid": 2,
            "seq": 1,
            "msg": {
                "index_id": "BRTI",
                "received_at": 0,
                "data": json.dumps({"value": f"{value:.2f}"}),
                "avg_60s_data": {
                    "value": "1",
                    "window_size": 60,
                    "window_start_ts_ms": 0,
                    "window_end_ts_exclusive": 0,
                },
            },
        }
    )


def world(
    spot: float = 82_000.0,
    strike: float = 82_000.0,
    *,
    yes_bid: str = "0.48",
    no_bid: str = "0.50",
    vol: float = 3e-5,
    secs: int = 1000,
    now_ms: int = NOW,
) -> LiveState:
    s = LiveState()
    s.markets["M"] = MarketInfo("M", "KXBTC15M", T0, CLOSE, "greater_or_equal", strike, None)
    s.on_message(snapshot("M", [(yes_bid, "500")], [(no_bid, "500")]), now_ms)
    for i in range(secs):  # 1 Hz index with a fixed per-step move to set vol
        v = spot * (1 + vol * (1 if i % 2 else -1) / 2)
        s.on_message(index_tick(v if i < secs - 1 else spot), now_ms - (secs - 1 - i) * 1000)
    return s


def test_state_book_levels_and_index() -> None:
    s = world()
    assert s.asks("M", "yes") == [Level(0.50, 500.0)]  # 1 - best NO bid
    assert s.asks("M", "no") == [Level(0.52, 500.0)]
    assert s.bids("M", "yes") == [Level(0.48, 500.0)]
    assert s.index_now() is not None
    assert s.sigma_per_s(NOW) == pytest.approx(3e-5, rel=0.1)
    assert s.asks("UNKNOWN", "yes") == []
    delta = msg(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 2,
            "msg": {
                "market_ticker": "M",
                "price_dollars": "0.48",
                "delta_fp": "-600.00",
                "side": "yes",
            },
        }
    )
    s.on_message(delta, NOW)  # would go negative: book resets instead of lying
    assert s.bids("M", "yes") == []
    rm = RestMarket.model_validate(market("X", open_time="2026-10-16T05:00:00Z"))
    s.set_market(rm, "KXBTCD")
    assert s.markets["X"].series == "KXBTCD"
    assert s.markets["X"].open_ts < s.markets["X"].close_ts


def test_fair_value_direction_and_window() -> None:
    cfg = EngineConfig()
    above = world(spot=82_200.0)
    p = fair_up(above, above.markets["M"], NOW, cfg)
    assert p is not None
    assert p > 0.9
    below = world(spot=81_800.0)
    assert (fair_up(below, below.markets["M"], NOW, cfg) or 1) < 0.1
    # final 30 s: average already printed well above the strike -> near certain UP
    late_ms = (CLOSE - 30) * 1000
    late = world(spot=82_100.0, now_ms=late_ms)
    assert (fair_up(late, late.markets["M"], late_ms, cfg) or 0) > 0.99
    empty = LiveState()
    assert fair_up(empty, above.markets["M"], NOW, cfg) is None


def test_calls_buy_up_buy_down_no_trade() -> None:
    cfg = EngineConfig()
    up = evaluate(world(spot=82_200.0), world().markets["M"], NOW, cfg)
    assert up.action == "BUY UP"
    assert "chance" in up.reason
    assert up.edge_up is not None
    assert up.edge_up >= cfg.min_edge
    down = evaluate(world(spot=81_800.0), world().markets["M"], NOW, cfg)
    assert down.action == "BUY DOWN"
    fair = evaluate(world(spot=82_000.0), world().markets["M"], NOW, cfg)
    assert fair.action == "NO TRADE"
    assert "Price is fair" in fair.reason


def test_lag_and_quality_gates_block_entries() -> None:
    cfg = EngineConfig()
    s = world(spot=82_200.0)
    m = s.markets["M"]
    stale = evaluate(s, m, NOW + 5_000, cfg)  # index 5 s old
    assert stale.action == "NO TRADE"
    assert "settlement index live" in stale.reason
    s2 = world(spot=82_200.0)
    s2.set_composite(
        CompositeSnapshot(NOW, 82_400.0, None, None, None, None, {}, None, 0.0, None, None), NOW
    )
    lagging = evaluate(s2, m, NOW, cfg)
    assert "exchanges in line with index" in lagging.reason
    wide = world(spot=82_200.0, yes_bid="0.30", no_bid="0.50")
    assert "spread" in evaluate(wide, m, NOW, cfg).reason
    late_ms = (CLOSE - 20) * 1000
    late = world(spot=82_200.0, now_ms=late_ms)
    assert "Too close" in evaluate(late, m, late_ms, cfg).reason


def test_hold_and_sell_advice() -> None:
    cfg = EngineConfig()
    s = world(spot=82_050.0, yes_bid="0.97", no_bid="0.01")  # market pays more than fair
    held = evaluate(s, s.markets["M"], NOW, cfg, Position("yes", 100, 0.50))
    assert held.action == "SELL"
    s2 = world(spot=82_200.0, yes_bid="0.60", no_bid="0.38")
    hold = evaluate(s2, s2.markets["M"], NOW, cfg, Position("yes", 100, 0.50))
    assert hold.action == "HOLD"
    no_buyers = world(spot=82_200.0)
    no_buyers.books["M"].reset()
    no_buyers.books["M"].apply_snapshot([], [(Decimal("0.5"), Decimal(10))])
    assert (
        evaluate(no_buyers, no_buyers.markets["M"], NOW, cfg, Position("yes", 1, 0.5)).action
        == "HOLD"
    )
    blind = LiveState()
    blind.markets["M"] = s.markets["M"]
    assert evaluate(blind, blind.markets["M"], NOW, cfg, Position("no", 1, 0.5)).action == "HOLD"


def test_paper_account_walks_depth_and_settles(tmp_path: Path) -> None:
    acct = PaperAccount(
        RiskConfig(starting_cash=500_000, max_risk_per_trade=0.01, max_per_market=25_000)
    )
    asks = [Level(0.50, 1000), Level(0.52, 100_000), Level(0.70, 100_000)]
    f = acct.buy("M", "yes", asks, max_price=0.60, equity=500_000, now_ms=NOW, reason="test")
    assert f is not None
    cost = 1000 * (0.50 + fee(0.50))
    extra = (5000 - cost) // (0.52 + fee(0.52))
    assert f.contracts == 1000 + extra  # budget = 1% of equity, never above max_price
    assert acct.cash == pytest.approx(500_000 - cost - extra * (0.52 + fee(0.52)))
    assert acct.buy("M", "yes", asks, 0.6, 500_000, NOW + 60_000, "again") is None  # no averaging
    assert acct.buy("N", "no", asks, 0.6, 500_000, NOW, "x") is not None
    assert acct.buy("N2", "no", asks, 0.6, 500_000, NOW, "x") is not None  # different market ok
    sold = acct.sell("N", [Level(0.55, 10_000)], NOW, "take profit")
    assert sold is not None
    assert sold.pnl is not None
    settled = acct.settle("M", "yes", NOW)
    assert settled is not None
    assert settled.pnl
    assert settled.pnl > 0
    assert acct.settle("M", "yes", NOW) is None
    path = tmp_path / "paper.json"
    acct.save(path)
    again = PaperAccount.load(path)
    assert again.cash == pytest.approx(acct.cash)
    assert again.position("N2") is not None
    assert PaperAccount.load(tmp_path / "missing.json").cash == 500_000


def test_daily_loss_limit_halts_entries(tmp_path: Path) -> None:
    acct = PaperAccount(RiskConfig(starting_cash=1000))
    acct.roll_day("2026-10-16", 1000)
    assert acct.halted(940)
    assert not acct.halted(960)


def test_engine_tick_trades_publishes_and_settles(tmp_path: Path) -> None:
    s = world(spot=82_200.0)
    eng = LiveEngine(s, tmp_path / "paper.json", clock=lambda: NOW)
    calls = eng.tick()
    assert calls[0].action == "BUY UP"
    assert "M" in eng.account.holdings
    live = eng.live_payload()
    assert live["calls"][0]["ticker"] == "M"
    assert live["index_age_ms"] == 0
    paper = eng.paper_payload()
    assert paper["holdings"][0]["side"] == "UP"
    eng.clock = lambda: CLOSE * 1000 + 1
    eng.tick()
    assert eng.pending_settlement == {"M"}
    eng.settle("M", "yes")
    assert eng.account.holdings == {}
    assert eng.paper_payload()["closed_trades"] == 1
    eng.save()
    assert (tmp_path / "paper.json").is_file()


def test_engine_shows_nearest_hourly_strikes(tmp_path: Path) -> None:
    s = world(spot=82_000.0)
    for k in range(10):
        tk = f"H{k}"
        s.markets[tk] = MarketInfo(tk, "KXBTCD", T0, T0 + 3600, "greater", 81_000.0 + 250 * k, None)
    eng = LiveEngine(s, tmp_path / "p.json", clock=lambda: NOW)
    shown = [m.ticker for m in eng.live_markets(NOW)]
    assert shown[0] == "M"
    assert len(shown) == 1 + 6
    assert "H4" in shown  # 82,000 strike is nearest
