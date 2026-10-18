"""Replaying the long-shot seller on recorded ticks and scoring cancel rules."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from edgebook.research import longshot_fills as lf
from edgebook.research.longshot_gaps import seller_pnl

START_MS = 1_792_130_400_000
CLOSE_S = START_MS // 1000 + 1800  # 30 minutes left at the start
TK = "KXBTCD-26OCT1606-T81999.99"
FIRST_BUY_MS = START_MS + 20_000
FILL_MS = START_MS + 40_000


def write(directory: Path, stream: str, rows: list[dict[str, Any]]) -> None:
    part = directory / stream / "date=2026-10-16"
    part.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), part / f"part-{START_MS}-1.parquet")


def level(
    ts: int, side: str | None, price: str | None, qty: str | None, kind: str
) -> dict[str, Any]:
    return {"recv_ts_ms": ts, "kind": kind, "ticker": TK, "side": side, "price": price, "qty": qty}


def recording(tmp_path: Path, result: str = "no") -> Path:
    d = tmp_path / "data"
    market = {
        "series": "KXBTCD",
        "ticker": TK,
        "close_time": datetime.fromtimestamp(CLOSE_S, tz=UTC),
        "strike_type": "greater",
        "floor_strike": 82_000.0,
        "cap_strike": None,
    }
    write(d, "markets", [{**market, "result": ""}])
    write(d, "settlements", [{**market, "result": result}])
    # UP bid 4c, DOWN bid 94c (= UP asked at 6c): UP is the long shot, we rest a DOWN bid
    write(
        d,
        "book",
        [
            level(START_MS, None, None, None, "reset"),
            level(START_MS, "yes", "0.0400", "100", "level"),
            level(START_MS, "no", "0.9400", "50", "level"),
            level(START_MS + 30_000, "yes", "0.0500", "10", "delta"),  # long-shot bid steps up
        ],
    )
    index = []
    for i in range(240):
        v = 81_000.0 + i  # rising toward the 82,000 strike
        index.append(
            {"recv_ts_ms": START_MS + i * 1000, "index_id": "BRTI", "raw": json.dumps({"value": v})}
        )
    write(d, "index", index)
    buy = {"ticker": TK, "yes_price": 0.06, "taker_side": "yes"}  # takers buying the long shot
    write(
        d,
        "trades",
        [
            {"recv_ts_ms": FIRST_BUY_MS, **buy, "count": 30.0},  # eats 30 of the 50 ahead of us
            {"recv_ts_ms": FILL_MS, **buy, "count": 40.0},  # last 20 ahead, then 20 to us
        ],
    )
    return d


def test_replay_joins_the_queue_and_fills_only_from_prints(tmp_path: Path) -> None:
    d = recording(tmp_path)
    eps = lf.replay_episodes(d, START_MS, START_MS + 120_000)
    assert len(eps) == 1
    ep = eps[0]
    assert (ep.side, ep.first_price, ep.queue_at_join) == ("no", 0.94, 50.0)
    assert ep.long_side == "yes"
    assert ep.first_fill_ms == FILL_MS
    assert ep.fill_price == 0.94
    assert ep.filled_contracts == pytest.approx(20.0)
    assert ep.result == "no"
    assert not ep.won_long
    assert ep.pnl == pytest.approx(seller_pnl(0.06, False))
    pre = ep.before(FILL_MS)
    assert pre is not None
    assert pre.t_ms <= FILL_MS
    assert pre.buy_30 == pytest.approx(30.0)  # the first long-shot buy, not the fill itself
    assert pre.queue == pytest.approx(20.0)
    assert pre.level == pytest.approx(50.0)
    assert pre.bid_up_30 == pytest.approx(1.0)  # 4c -> 5c
    assert pre.toward_30 is not None
    assert pre.toward_30 > 0
    assert pre.dist_bps is not None
    assert pre.dist_bps > 0
    assert pre.sell_60 == 0
    # samples stop at the first fill
    assert all(s.t_ms <= FILL_MS + 1000 for s in ep.samples)


def test_replay_span_defaults_to_the_trade_tape(tmp_path: Path) -> None:
    d = recording(tmp_path)
    assert lf._span(d) == (FIRST_BUY_MS, FILL_MS)
    calls: list[int] = []
    eps = lf.replay_episodes(d, progress=calls.append)
    assert eps == [] or eps[0].ticker == TK


def test_research_spec_lifts_caps_but_keeps_the_rule() -> None:
    spec = lf.research_spec()
    assert spec.mode == "longshot"
    assert spec.longshot.max_event_cost == float("inf")
    assert spec.longshot.min_secs_left == 900.0
    assert spec.risk is not None
    assert spec.risk.max_per_market == 2_500.0
    with pytest.raises(ValueError, match="not a maker"):
        lf.replay_episodes(Path("nowhere"), 0, 1, spec=replace(spec, mode="taker"))


def test_toward_sign_follows_strike_type_and_side() -> None:
    assert lf._toward_sign("greater", "yes") == 1.0
    assert lf._toward_sign("greater_or_equal", "no") == -1.0
    assert lf._toward_sign("less", "yes") == -1.0
    assert lf._toward_sign("between", "yes") is None


# ------------------------------------------------------------------ scoring


def sample(t_ms: int, buy_30: float = 0.0, **kw: Any) -> lf.Sample:
    base: dict[str, Any] = {
        "t_ms": t_ms,
        "secs_left": 1800.0,
        "price": 0.94,
        "queue": 10.0,
        "level": 10.0,
        "long_bid": 0.04,
        "buy_10": 0.0,
        "buy_30": buy_30,
        "buy_60": 0.0,
        "buy_300": 0.0,
        "sell_60": 0.0,
        "bid_up_30": 0.0,
        "bid_up_60": 0.0,
        "level_drop_60": 0.0,
        "toward_30": 0.0,
        "toward_60": 0.0,
        "toward_300": 0.0,
        "dist_bps": 50.0,
    }
    base.update(kw)
    return lf.Sample(**base)


def episode(
    ticker: str,
    close_ts: int,
    result: str,
    fill_ms: int | None,
    samples: list[lf.Sample],
    placed_ms: int = START_MS,
) -> lf.Episode:
    return lf.Episode(
        ticker=ticker,
        event=ticker.rsplit("-", 1)[0],
        close_ts=close_ts,
        side="no",
        placed_ms=placed_ms,
        first_price=0.94,
        queue_at_join=10.0,
        first_fill_ms=fill_ms,
        fill_price=0.94 if fill_ms is not None else None,
        filled_contracts=5.0 if fill_ms is not None else 0.0,
        result=result,
        samples=samples,
    )


def toy_episodes() -> list[lf.Episode]:
    """Two closes. Bad fills (long shot won) follow a burst of long-shot buying."""
    out = []
    for c, close in enumerate((CLOSE_S, CLOSE_S + 3600)):
        ev = f"KXBTCD-E{c}"
        t = START_MS + c * 3_600_000
        burst = [sample(t), sample(t + 1000, buy_30=500.0)]
        calm = [sample(t), sample(t + 1000)]
        out += [
            episode(f"{ev}-T1", close, "yes", t + 5000, burst, t),  # adverse: avoidable
            episode(f"{ev}-T2", close, "no", t + 5000, calm, t),  # good fill: kept
            episode(f"{ev}-T3", close, "no", t + 1100, burst, t),  # good, too fast to cancel
            episode(f"{ev}-T4", close, "no", None, burst, t),  # never filled
        ]
    return out


def test_avoided_needs_the_rule_to_fire_before_the_fill_plus_latency() -> None:
    rule = lf.Rule("buy_30", 100.0)
    eps = toy_episodes()
    assert lf.trigger_ms(eps[0], rule) == START_MS + 1000
    assert lf.avoided(eps[0], rule)
    assert not lf.avoided(eps[1], rule)  # never fires
    assert not lf.avoided(eps[2], rule)  # fill arrived 100 ms after the trigger
    assert lf.avoided(eps[2], rule, latency_ms=50)
    assert not lf.avoided(eps[3], rule)  # nothing to avoid
    assert not lf.Rule("toward_60", 1.0).fires(sample(0, toward_60=None))


def test_score_rule_compares_kept_and_avoided_fills() -> None:
    eps = toy_episodes()
    res = lf.score_rule(eps, lf.Rule("buy_30", 100.0))
    assert res["all_fills"]["n"] == 6
    assert res["kept"]["n"] == 4
    assert res["avoided"]["n"] == 2
    assert res["avoided"]["long_shot_win_rate"] == 1.0
    assert res["kept_minus_all_cents"]["mean"] > 0
    assert res["unfilled_orders_cancelled"] == 2
    assert res["unfilled_orders"] == 2
    empty = lf._diff_ci([], set())
    assert empty["mean"] is None


def test_choose_rule_prefers_the_rule_that_removes_bad_fills() -> None:
    rule, grid = lf.choose_rule(toy_episodes())
    assert rule is not None
    assert rule.feature == "buy_30"
    assert grid
    assert all(g["threshold"] > 0 for g in grid)
    assert lf.choose_rule([]) == (None, [])


def test_split_closes_is_chronological() -> None:
    eps = toy_episodes()
    early, late = lf.split_closes(eps)
    assert {e.close_ts for e in early} == {CLOSE_S}
    assert {e.close_ts for e in late} == {CLOSE_S + 3600}


def test_lead_table_and_overview() -> None:
    eps = toy_episodes()
    lead = lf.lead_table(eps)
    assert lead["filled_long_shot_won"]["n"] == 2
    assert lead["filled_long_shot_won"]["1s"]["buy_30"] == 500.0
    assert lead["filled_long_shot_lost"]["n"] == 4
    assert lead["unfilled_while_resting"]["n"] == 2
    assert lead["unfilled_while_resting"]["avg"]["buy_30"] == 250.0
    ov = lf.fill_overview(eps)
    assert ov["fill_rate"] == 0.75
    assert ov["median_secs_to_fill"] == 5.0
    assert lf._nanmean([None, float("nan")]) is None


def test_analyse_scores_the_frozen_rule_on_later_closes() -> None:
    eps = [*toy_episodes(), episode("KXBTCD-E9-T1", CLOSE_S, "", START_MS, [])]
    res = lf.analyse(eps)
    assert res["data"]["unsettled_skipped"] == 1
    assert res["data"]["explore_closes"] == 1
    assert res["data"]["test_closes"] == 1
    assert res["rule_chosen_on_explore"]["feature"] == "buy_30"
    assert res["rule_test"]["avoided"]["n"] == 1
    fixed = lf.analyse(eps, rule=lf.Rule("buy_10", 1.0))
    assert fixed["rule_evaluated"] == {"feature": "buy_10", "threshold": 1.0}
    assert fixed["rule_test"]["avoided"]["n"] == 0


def test_episodes_round_trip(tmp_path: Path) -> None:
    eps = toy_episodes()
    path = tmp_path / "eps.json"
    lf.save_episodes(eps, path)
    assert lf.load_episodes(path) == eps


def test_compare_with_paper_matches_first_buys_per_market() -> None:
    eps = toy_episodes()
    paper = {
        "started_ms": START_MS - 1,
        "fills": [
            {"ticker": "KXBTCD-E0-T1", "action": "BUY", "ts_ms": START_MS + 4000, "price": 0.94},
            {"ticker": "KXBTCD-E0-T1", "action": "BUY", "ts_ms": START_MS + 9000, "price": 0.95},
            {"ticker": "KXBTCD-E0-T4", "action": "BUY", "ts_ms": START_MS + 4000, "price": 0.94},
            {"ticker": "KXBTCD-E0-T2", "action": "SETTLE", "ts_ms": START_MS, "price": 1.0},
            {"ticker": "OTHER", "action": "BUY", "ts_ms": 1, "price": 0.9},  # outside the replay
        ],
    }
    res = lf.compare_with_paper(eps, paper)
    assert res["sim_markets"] == 8
    assert res["paper_filled_markets"] == 2
    assert res["both_filled"] == 1
    assert res["paper_only"] == 1  # T4: the paper account filled, the replay did not
    assert res["sim_only"] == 5
    assert res["median_fill_lag_s"] == 1.0
    assert res["price_matches"] == 1
