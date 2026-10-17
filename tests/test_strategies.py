"""Strategy accounts: market-anchored fair value, exits, maker fills, side-by-side
paper accounts, the settlement fix, the decision log and the weight fit."""

import json
import random
from pathlib import Path
from typing import Any, ClassVar

import pytest

from edgebook.app import cli
from edgebook.config import Settings
from edgebook.core.types import LiquidityRole
from edgebook.engine.decide import EngineConfig, Position, blend_fair, evaluate, fair_up, fee
from edgebook.engine.loop import DEFAULT_STRATEGIES, LiveEngine, StrategySpec
from edgebook.engine.maker import MakerBook, MakerConfig, maker_bid
from edgebook.engine.paper import Holding, PaperAccount, RiskConfig
from edgebook.engine.state import LiveState
from edgebook.research import strategy_report as srep
from tests.test_engine import NOW, msg, world

ANCHORED = EngineConfig(model_weight=0.2, exit_mode="take_profit", take_profit=0.03)
MAKER_FEE = LiquidityRole.MAKER


# ---------------------------------------------------------------- fair value


def test_blend_fair_moves_from_market_to_model() -> None:
    assert blend_fair(0.9, 0.5, 0.0) == pytest.approx(0.5)
    assert blend_fair(0.9, 0.5, 1.0) == pytest.approx(0.9)
    mids = [blend_fair(0.9, 0.5, w / 10) for w in range(11)]
    assert mids == sorted(mids)
    assert blend_fair(0.9999, 0.999, 1.0) == 0.99  # clipped
    assert blend_fair(0.0, 0.0001, 1.0) == 0.01


def test_anchored_call_uses_blend_and_keeps_raw_model() -> None:
    s = world(spot=82_200.0, yes_bid="0.48", no_bid="0.51")
    c = evaluate(s, s.markets["M"], NOW, ANCHORED)
    assert c.model_up is not None
    assert c.model_up > 0.9
    assert c.market_up == pytest.approx(0.485)
    assert c.fair_up == pytest.approx(blend_fair(c.model_up, 0.485, 0.2))
    assert c.action == "BUY UP"
    assert "model" in c.reason
    assert "Kalshi" in c.reason
    # no two-sided market -> no anchor -> no fair value, no trade
    s.books["M"].reset()
    blind = evaluate(s, s.markets["M"], NOW, ANCHORED)
    assert blind.fair_up is None
    assert blind.action == "NO TRADE"


def test_take_profit_exit_holds_until_bid_clears_fair_plus_margin() -> None:
    cfg_fair = EngineConfig()
    cfg_tp = EngineConfig(exit_mode="take_profit", take_profit=0.03)
    probe = world(spot=82_030.0)
    p = fair_up(probe, probe.markets["M"], NOW, cfg_fair)
    assert p is not None
    assert 0.55 < p < 0.95
    # bid - fee (fees round up to the cent) lands just above fair: fair-mode sells, tp holds
    near = f"{round(p + 0.03, 2):.2f}"
    s = world(spot=82_030.0, yes_bid=near, no_bid="0.01")
    pos = Position("yes", 100, 0.50)
    assert evaluate(s, s.markets["M"], NOW, cfg_fair, pos).action == "SELL"
    held = evaluate(s, s.markets["M"], NOW, cfg_tp, pos)
    assert held.action == "HOLD"
    assert "Holding UP to the result" in held.reason
    rich = world(spot=82_030.0, yes_bid="0.99", no_bid="0.00")
    sold = evaluate(rich, rich.markets["M"], NOW, cfg_tp, pos)
    assert sold.action == "SELL"
    assert sold.reason.startswith("Take profit")
    assert sold.sell_up_at is not None
    assert sold.sell_up_at - fee(sold.sell_up_at) >= p + 0.03 - 0.01


# ---------------------------------------------------------------- maker


def test_maker_bid_respects_edge_and_spread() -> None:
    q = maker_bid(0.60, best_bid=0.50, best_ask=0.52, edge=0.02)
    assert q == 0.51  # capped one cent above the bid
    assert maker_bid(0.60, best_bid=0.50, best_ask=0.51, edge=0.02) == 0.50  # never crosses
    q2 = maker_bid(0.40, best_bid=0.50, best_ask=0.52, edge=0.02)
    assert q2 is not None
    assert 0.40 - q2 - fee(q2, MAKER_FEE) >= 0.02 - 1e-9
    assert maker_bid(0.02, 0.01, 0.03, 0.02) is None


def test_trade_messages_reach_state() -> None:
    s = LiveState()
    s.on_message(
        msg(
            {
                "type": "trade",
                "sid": 3,
                "seq": 1,
                "msg": {
                    "trade_id": "t1",
                    "market_ticker": "M",
                    "yes_price_dollars": "0.45",
                    "count_fp": "10.00",
                    "taker_side": "no",
                    "ts_ms": 1,
                },
            }
        ),
        NOW,
    )
    [t] = s.trades_after(0)
    assert (t.ticker, t.yes_price, t.count, t.taker_side, t.ts_ms) == ("M", 0.45, 10.0, "no", NOW)
    assert s.trades_after(t.seq) == []


def maker_world() -> tuple[LiveState, Any]:
    s = world(spot=82_200.0, yes_bid="0.48", no_bid="0.51")  # UP 0.48 bid / 0.49 ask
    return s, evaluate(s, s.markets["M"], NOW, ANCHORED)


def test_maker_fills_only_from_real_prints_after_the_queue() -> None:
    s, call = maker_world()
    acct = PaperAccount()
    book = MakerBook()
    o = book.quote(s, acct, call, MakerConfig(), acct.cash, NOW, allowed=True)
    assert o is not None
    assert (o.side, o.price, o.queue_ahead) == ("yes", 0.48, 500.0)  # joins the 500 at 0.48
    s.add_trade("M", 0.48, 300, "yes", NOW)  # a buyer: never fills a bid
    s.add_trade("OTHER", 0.40, 300, "no", NOW)  # another market
    s.add_trade("M", 0.48, 300, "no", NOW)  # seller at our price: queue first
    assert book.match(s, acct) == []
    assert o.queue_ahead == 200
    s.add_trade("M", 0.48, 300, "no", NOW + 1)  # 200 queue, then 100 to us
    [f1] = book.match(s, acct)
    assert f1.contracts == 100
    assert f1.price == 0.48
    assert f1.fees == pytest.approx(100 * fee(0.48, MAKER_FEE))
    s.add_trade("M", 0.47, 50, "no", NOW + 2)  # through our price
    [f2] = book.match(s, acct)
    assert f2 is f1  # same order: one trade record, not one per print
    assert f2.contracts == 150
    assert len(acct.fills) == 1
    h = acct.holdings["M"]
    assert h.contracts == 150
    assert h.cost == pytest.approx(150 * (0.48 + fee(0.48, MAKER_FEE)))
    assert acct.cash == pytest.approx(500_000 - h.cost)
    # holding open: the same order keeps working, it never flips side
    assert book.quote(s, acct, call, MakerConfig(), acct.cash, NOW, allowed=True) is o
    assert book.quote(s, acct, call, MakerConfig(), acct.cash, NOW, allowed=False) is None
    assert book.orders == {}


def test_maker_requotes_on_price_change_and_cancels_when_blocked() -> None:
    s, call = maker_world()
    acct = PaperAccount()
    book = MakerBook()
    o = book.quote(s, acct, call, MakerConfig(), acct.cash, NOW, True)
    assert o is not None
    assert book.quote(s, acct, call, MakerConfig(), acct.cash, NOW, True) is o  # unchanged
    wider = MakerConfig(edge=0.35)
    o2 = book.quote(s, acct, call, wider, acct.cash, NOW, True)
    assert o2 is not None
    assert o2.price < o.price  # new price -> new order, back of the queue
    assert book.quote(s, acct, call, MakerConfig(edge=0.9), acct.cash, NOW, True) is None
    assert book.quote(s, acct, call, MakerConfig(), 0.0, NOW, True) is None  # no budget
    assert book.orders == {}


# ---------------------------------------------------------------- engine


def test_engine_runs_strategies_side_by_side(tmp_path: Path) -> None:
    old = PaperAccount()
    old.add_fill("OLD", "yes", 10, 0.5, 0.01, NOW - 10_000, "day one")
    old.save(tmp_path / "paper.json")
    (tmp_path / "model_weight.json").write_text(json.dumps({"model_weight": 0.35, "markets": 99}))
    s = world(spot=82_200.0, yes_bid="0.48", no_bid="0.51")
    eng = LiveEngine(
        s,
        tmp_path / "paper.json",
        clock=lambda: NOW,
        strategies=DEFAULT_STRATEGIES,
        decision_log_dir=tmp_path / "decisions",
    )
    by = {r.spec.name: r for r in eng.runners}
    assert by["v1"].account.fills[0].reason == "day one"  # v1 history continues
    assert by["v1"].cfg.model_weight == 1.0
    assert by["anchored"].cfg.model_weight == 0.35  # fitted weight applied
    assert by["maker"].cfg.model_weight == 0.35
    assert eng.primary is by["anchored"]
    calls = eng.tick()
    assert calls[0].action == "BUY UP"
    assert "M" in by["v1"].account.holdings
    assert "M" in by["anchored"].account.holdings
    maker_call = next(c for c in by["maker"].calls if c.ticker == "M")
    assert maker_call.action == "BID UP"
    assert "M" not in by["maker"].account.holdings
    s.add_trade("M", 0.47, 1000, "no", NOW)
    eng.tick()
    assert by["maker"].account.holdings["M"].contracts > 0
    eng.save()
    for f in ("paper.json", "paper-anchored.json", "paper-anchored50.json", "paper-maker.json"):
        assert (tmp_path / f).is_file()
    payload = eng.strategies_payload()
    assert [a["name"] for a in payload["accounts"]] == [
        "v1",
        "anchored",
        "anchored50",
        "maker",
        "longshot",
    ]
    assert payload["primary"] == "anchored"
    assert payload["fit"]["markets"] == 99
    live = eng.live_payload()
    assert live["strategy"] == "anchored"
    assert eng.paper_payload("maker")["mode"] == "maker"
    rows = (tmp_path / "decisions").glob("*.jsonl")
    lines = [json.loads(x) for p in rows for x in p.read_text().splitlines()]
    assert lines
    assert lines[0]["ticker"] == "M"
    assert lines[0]["model_up"] is not None
    with pytest.raises(ValueError, match="unique"):
        LiveEngine(s, tmp_path / "x.json", strategies=[DEFAULT_STRATEGIES[0]] * 2)


def test_held_market_missing_after_restart_still_settles(tmp_path: Path) -> None:
    acct = PaperAccount()
    acct.cash -= 50.0
    acct.holdings["GONE"] = Holding("yes", 100, 50.0, NOW - 3_600_000)
    acct.save(tmp_path / "paper.json")
    s = world(spot=82_000.0)  # "GONE" is not a listed market any more
    eng = LiveEngine(s, tmp_path / "paper.json", clock=lambda: NOW)
    eng.tick()
    assert eng.pending_settlement == {"GONE"}
    eng.settle("GONE", "yes")
    assert eng.account.holdings == {}
    assert eng.account.realized == pytest.approx(50.0)
    assert PaperAccount.load(tmp_path / "paper.json").holdings == {}


def test_maker_strategy_halts_and_waits(tmp_path: Path) -> None:
    spec = next(s for s in DEFAULT_STRATEGIES if s.mode == "maker")
    s = world(spot=82_000.0)
    eng = LiveEngine(
        s,
        tmp_path / "p.json",
        clock=lambda: NOW,
        strategies=[spec],
        risk=RiskConfig(daily_loss_limit=0.05),
    )
    eng.account.day = "2026-10-16"
    eng.account.day_start_equity = 1_000_000.0  # way above equity -> halted
    eng.account.roll_day = lambda day, equity: None  # type: ignore[method-assign]
    [c] = [c for c in eng.tick() if c.ticker == "M"]
    assert c.action == "NO TRADE"
    assert "Daily loss limit" in c.reason
    stale = LiveEngine(
        world(spot=82_000.0), tmp_path / "q.json", clock=lambda: NOW + 9_000, strategies=[spec]
    )
    [c2] = [c for c in stale.tick() if c.ticker == "M"]
    assert c2.reason.startswith("Waiting")
    assert stale.runners[0].maker is not None
    assert stale.runners[0].maker.orders == {}


def test_single_strategy_engine_is_backward_compatible(tmp_path: Path) -> None:
    eng = LiveEngine(world(), tmp_path / "p.json", cfg=EngineConfig(min_edge=0.05))
    assert [r.spec.name for r in eng.runners] == ["v1"]
    assert eng.cfg.min_edge == 0.05
    spec = StrategySpec("only", "Only", "")
    assert LiveEngine(world(), tmp_path / "q.json", strategies=[spec]).primary.spec is spec


# ---------------------------------------------------------------- report / fit


def synthetic_rows(model_good: bool, n: int = 600) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rng = random.Random(7)  # noqa: S311 - reproducible test data
    rows, results = [], {}
    for i in range(n):
        p = rng.uniform(0.1, 0.9)
        noisy = min(max(p + rng.gauss(0, 0.25), 0.02), 0.98)
        model, market = (p, noisy) if model_good else (noisy, p)
        tk = f"T{i}"
        results[tk] = "yes" if rng.random() < p else "no"
        rows.append(
            {
                "ts_ms": i * 60_000,
                "ticker": tk,
                "seconds_left": 300,
                "gates_ok": True,
                "model_up": model,
                "market_up": market,
            }
        )
    return rows, results


def test_fit_model_weight_finds_who_to_trust() -> None:
    rows, res = synthetic_rows(model_good=False)
    fit = srep.fit_model_weight(rows, res)
    assert fit is not None
    assert fit.model_weight <= 0.2
    assert fit.enough_data
    assert fit.test_logloss_market < fit.test_logloss_model
    rows2, res2 = synthetic_rows(model_good=True)
    fit2 = srep.fit_model_weight(rows2, res2)
    assert fit2 is not None
    assert fit2.model_weight >= 0.7
    assert srep.fit_model_weight(rows, {}) is None


def test_usable_rows_drop_gaps_duplicates_and_late_rows() -> None:
    base = {"ticker": "A", "model_up": 0.5, "market_up": 0.5, "gates_ok": True, "seconds_left": 300}
    rows = [
        {**base, "ts_ms": 0},
        {**base, "ts_ms": 10_000},  # same minute
        {**base, "ts_ms": 60_000, "gates_ok": False},
        {**base, "ts_ms": 120_000, "seconds_left": 10},
        {**base, "ts_ms": 180_000, "market_up": None},
        {**base, "ts_ms": 240_000},
    ]
    assert [r["ts_ms"] for r in srep.usable(rows)] == [0, 240_000]


def test_account_report_and_hold_counterfactual(tmp_path: Path) -> None:
    fills = [
        {
            "ticker": "A",
            "action": "BUY",
            "side": "yes",
            "contracts": 100,
            "price": 0.60,
            "fees": 2.0,
            "reason": "Model gives UP a 70% chance; it costs 0.60.",
            "pnl": None,
        },
        {
            "ticker": "A",
            "action": "SELL",
            "side": "yes",
            "contracts": 100,
            "price": 0.40,
            "fees": 2.0,
            "reason": "x",
            "pnl": -24.0,
        },
        {
            "ticker": "B",
            "action": "BUY",
            "side": "no",
            "contracts": 50,
            "price": 0.30,
            "fees": 0.5,
            "reason": "Fair value 40% for DOWN (model 50%, Kalshi 31%)",
            "pnl": None,
        },
        {
            "ticker": "B",
            "action": "BUY",
            "side": "no",
            "contracts": 50,
            "price": 0.32,
            "fees": 0.5,
            "reason": "Resting DOWN bid at 0.32 filled (fair value 0.40).",
            "pnl": None,
        },
        {
            "ticker": "B",
            "action": "SETTLE",
            "side": "no",
            "contracts": 100,
            "price": 1.0,
            "fees": 0.0,
            "reason": "Market settled NO",
            "pnl": 68.0,
        },
    ]
    rep = srep.account_report("t", {"fills": fills, "realized": 44.0}, {"A": "yes", "B": "no"})
    assert rep.trades == 2
    assert rep.per_contract.mean == pytest.approx((-0.24 + 0.68) / 2)
    assert rep.hold_per_contract.mean == pytest.approx(((1 - 0.6 - 0.02) + (1 - 0.31 - 0.01)) / 2)
    assert rep.model_said == pytest.approx(0.55)
    assert rep.won == 1.0
    assert srep._fair_from_reason("fair value 0.42") == 0.42
    assert srep._fair_from_reason("nothing") is None
    (tmp_path / "2026-10-17.jsonl").write_text('{"a": 1}\n{torn')
    assert srep.load_decisions(tmp_path) == [{"a": 1}]
    out = srep.report_json(None, [rep])
    assert out["fit"] is None
    assert out["accounts"][0]["name"] == "t"


def test_strategy_report_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine_dir = tmp_path / "engine"
    a = PaperAccount()
    a.add_fill("A", "yes", 10, 0.5, 0.01, NOW, "Model gives UP a 60% chance")
    a.settle("A", "yes", NOW + 1)
    a.save(engine_dir / "paper.json")
    PaperAccount().save(engine_dir / "paper-maker.json")
    rows, res = synthetic_rows(model_good=False, n=60)
    (engine_dir / "decisions").mkdir()
    (engine_dir / "decisions" / "d.jsonl").write_text("\n".join(json.dumps(r) for r in rows))

    async def fake_results(settings: Any, tickers: set[str]) -> dict[str, str]:
        return {**res, "A": "yes"}

    monkeypatch.setattr(cli, "market_results", fake_results)
    monkeypatch.setenv("EDGEBOOK_DATA_DIR", str(tmp_path))
    assert cli.main(["strategy-report", "--write-weight"]) == 0
    out = capsys.readouterr().out
    assert '"name": "v1"' in out
    assert '"name": "maker"' in out
    assert "wrote model_weight=" in out
    assert json.loads((engine_dir / "model_weight.json").read_text())["markets"] == 60


def test_market_results_caches_and_skips_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class M:
        def __init__(self, result: str) -> None:
            self.result = result

    class FakeRest:
        calls: ClassVar[list[str]] = []

        def __init__(self, url: str) -> None:
            pass

        async def market(self, ticker: str) -> M:
            FakeRest.calls.append(ticker)
            if ticker == "BAD":
                raise RuntimeError("404")
            return M("yes" if ticker == "DONE" else "")

    monkeypatch.setattr(cli, "KalshiRest", FakeRest)
    monkeypatch.setenv("EDGEBOOK_DATA_DIR", str(tmp_path))
    import asyncio

    got = asyncio.run(cli.market_results(Settings(), {"DONE", "OPEN", "BAD"}))
    assert got == {"DONE": "yes"}
    FakeRest.calls.clear()
    asyncio.run(cli.market_results(Settings(), {"DONE", "OPEN"}))
    assert FakeRest.calls == ["OPEN"]  # settled results come from the cache
