from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from edgebook.engine.decide import blend_fair
from edgebook.engine.maker import LongshotConfig
from edgebook.research.longshot_fair_value import (
    Entry,
    LoggedFiring,
    analyse,
    bar,
    choose_margin,
    diff_ci,
    firing_from_row,
    load_firings,
    paper_entries,
    policy_entries,
    split_events,
    verdict,
    won_long,
)
from edgebook.research.longshot_gaps import seller_pnl

T0 = 1_792_000_000_000
HOUR = 3_600_000


def row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts_ms": T0,
        "ticker": "KXBTCD-26OCT1713-T82000",
        "seconds_left": 1800.0,
        "model_up": 0.02,
        "market_up": 0.035,
        "up_ask": 0.04,
        "up_bid": 0.03,
    }
    return base | kw


def fire(ticker: str, ts: int, edge: float, up: bool = True, ask: float = 0.05) -> LoggedFiring:
    event = ticker.rsplit("-", 1)[0]
    close = (ts // HOUR + 1) * HOUR
    return LoggedFiring(event, ticker, ts, close, up, ask, ask - edge, edge)


def test_firing_from_row_mirrors_the_rule() -> None:
    f = firing_from_row(row())
    assert f is not None
    fair = blend_fair(0.02, 0.035, 0.2)
    assert (f.long_is_up, f.long_ask, f.event) == (True, 0.04, "KXBTCD-26OCT1713")
    assert f.fair_long == pytest.approx(fair)
    assert f.edge == pytest.approx(0.04 - fair)
    assert f.close_ms == round((T0 + 1_800_000) / 60_000) * 60_000
    jitter = firing_from_row(row(ts_ms=T0 + 30_400, seconds_left=1769.6))
    assert jitter is not None
    assert jitter.close_ms == f.close_ms  # one close, whatever the log timestamp
    down = firing_from_row(row(up_ask=0.97, up_bid=0.95, model_up=0.97, market_up=0.96))
    assert down is not None
    assert (down.long_is_up, down.long_ask) == (False, 0.05)
    assert down.fair_long == pytest.approx(1 - blend_fair(0.97, 0.96, 0.2))
    assert firing_from_row(row(seconds_left=600.0)) is None  # inside the 15-minute cut
    assert firing_from_row(row(up_ask=0.5, up_bid=0.49)) is None  # no long shot
    assert firing_from_row(row(model_up=None)) is None
    wide = LongshotConfig(min_secs_left=300.0)
    assert firing_from_row(row(seconds_left=600.0), wide) is not None


def test_firing_from_the_longshot_strategy_log() -> None:
    logged = {
        "ts_ms": T0,
        "ticker": "KXBTC-26OCT1713-B82125",
        "seconds_left": 1800.0,
        "long_is_up": False,
        "long_ask": 0.06,
        "fair_long": 0.05,
        "data_ok": True,
    }
    f = firing_from_row(logged)
    assert f is not None
    assert (f.long_is_up, f.long_ask, f.fair_long) == (False, 0.06, 0.05)
    assert f.edge == pytest.approx(0.01)
    assert firing_from_row(logged | {"data_ok": False}) is None
    assert firing_from_row(logged | {"seconds_left": 120.0}) is None


def test_load_firings(tmp_path: Path) -> None:
    p = tmp_path / "d.jsonl"
    p.write_text(
        "\n".join(json.dumps(r) for r in (row(ts_ms=T0 + 5), row(), row(up_ask=0.5, up_bid=0.4)))
    )
    fs = load_firings([p])
    assert [f.ts_ms for f in fs] == [T0, T0 + 5]


def test_won_long() -> None:
    assert won_long(True, "yes")
    assert won_long(False, "no")
    assert not won_long(True, "no")
    assert not won_long(False, "yes")


def test_policy_entries_take_the_first_kept_firing_per_market() -> None:
    fs = [fire("E-A", T0, 0.001), fire("E-A", T0 + 10, 0.02), fire("E-B", T0, -0.01)]
    fs.append(fire("E-C", T0, 0.03))  # unsettled: dropped
    res = {"E-A": "no", "E-B": "yes"}
    cur = policy_entries(fs, res, 0.0)
    assert [(e.ticker, e.edge) for e in cur] == [("E-A", 0.001)]
    assert cur[0].pnl == pytest.approx(seller_pnl(0.05, False))
    strict = policy_entries(fs, res, 0.01)
    assert [(e.ticker, e.edge) for e in strict] == [("E-A", 0.02)]  # a later, stronger firing
    band = policy_entries(fs, res, None)
    assert {e.ticker for e in band} == {"E-A", "E-B"}
    assert next(e for e in band if e.ticker == "E-B").pnl == pytest.approx(seller_pnl(0.05, True))


def test_paper_entries_join_fills_to_the_latest_logged_firing() -> None:
    fs = [
        fire("E-A", T0, 0.001),
        fire("E-A", T0 + 60_000, 0.004),
        fire("E-A", T0 + 60_000, 0.009, up=False),  # other side: ignored
        fire("E-B", T0 - 700_000, 0.01),  # too long before the fill
    ]
    account = {
        "fills": [
            {"action": "BUY", "ticker": "E-A", "side": "no", "ts_ms": T0 + 90_000},
            {"action": "BUY", "ticker": "E-A", "side": "no", "ts_ms": T0 + 80_000},
            {"action": "SETTLE", "ticker": "E-A", "contracts": 100.0, "pnl": 4.0},
            {"action": "BUY", "ticker": "E-B", "side": "no", "ts_ms": T0},
            {"action": "SETTLE", "ticker": "E-B", "contracts": 10.0, "pnl": -9.0},
            {"action": "BUY", "ticker": "E-C", "side": "no", "ts_ms": T0},  # still open
        ]
    }
    [e] = paper_entries(account, fs)
    assert (e.ticker, e.edge) == ("E-A", 0.004)
    assert e.pnl == pytest.approx(0.04)


def entries(spec: list[tuple[str, int, float]], edge: float = 0.01) -> list[Entry]:
    return [Entry(ev, f"{ev}-{i}", close, edge, pnl) for i, (ev, close, pnl) in enumerate(spec)]


def test_split_events_and_choose_margin() -> None:
    es = entries([(f"E{i}", T0 + i * HOUR, 0.01) for i in range(7)])
    early, late = split_events(es)
    assert early == {"E0", "E1"}
    assert late == {f"E{i}" for i in range(2, 7)}
    dup = [*es, Entry("E2", "E2-x", T0 + 2 * HOUR + 60_000, 0.01, 0.0)]
    assert split_events(dup) == (early, late)  # an event is on one side only
    a = entries([("E0", T0, 0.02)] * 10)
    b = entries([("E0", T0, 0.03)] * 4)  # better but too few markets
    c = entries([("E0", T0, 0.02)] * 10)
    assert choose_margin({0.0: a, 0.005: b, 0.0025: c}) == 0.0  # tie -> smaller margin
    assert choose_margin({0.005: b}) is None


def test_bar_reports_the_pass_bar_numbers() -> None:
    es = entries([("E0", T0, 0.05), ("E0", T0, -0.95), ("E1", T0 + 23 * HOUR, 0.04)])
    b = bar(es)
    assert (b["trades"], b["closes"]) == (3, 2)
    assert b["mean_cents"] == pytest.approx(-86 / 3)
    assert b["trades_per_day"] == pytest.approx(3.0)
    assert (b["worst_close"], b["worst_close_cents"]) == ("E0", pytest.approx(-90))
    lo, hi = b["ci_cents"]
    assert lo <= b["mean_cents"] <= hi
    assert bar([]) == {"trades": 0, "closes": 0}


def test_diff_ci_and_verdict() -> None:
    base = entries([(f"E{i}", T0, -0.02 if i % 2 else 0.01) for i in range(20)])
    better = [Entry(e.event, e.ticker, e.close_ms, e.edge, e.pnl + 0.03) for e in base]
    d = diff_ci(better, base)
    assert d["diff_cents"] == pytest.approx(3.0)
    assert d["ci_cents"][0] > 0
    assert verdict(d, 20) == "YES"
    worse = diff_ci(base, better)
    assert verdict(worse, 5) == "NO"
    assert diff_ci([], base)["diff_cents"] is None
    assert verdict({"diff_cents": None, "ci_cents": [None, None]}, 40) == "NOT YET"
    assert verdict({"diff_cents": -0.5, "ci_cents": [-3.0, 2.0]}, 40) == "NO"
    assert verdict({"diff_cents": -0.5, "ci_cents": [-3.0, 2.0]}, 10) == "NOT YET"
    assert verdict({"diff_cents": 0.5, "ci_cents": [-3.0, 2.0]}, 40) == "NOT YET"


def test_analyse_end_to_end() -> None:
    fs: list[LoggedFiring] = []
    res: dict[str, str] = {}
    fills: list[dict[str, Any]] = []
    for h in range(12):
        ev = f"KXBTCD-H{h:02d}"
        for k in range(6):
            tk = f"{ev}-T{k}"
            edge = 0.012 if k < 3 else 0.001  # strong edges are the sales that win
            fs.append(fire(tk, T0 + h * HOUR + 1000, edge))
            res[tk] = "no" if k < 3 or k == 3 else "yes"
            fills += [
                {"action": "BUY", "ticker": tk, "side": "no", "ts_ms": T0 + h * HOUR + 2000},
                {
                    "action": "SETTLE",
                    "ticker": tk,
                    "contracts": 10.0,
                    "pnl": 0.5 if res[tk] == "no" else -9.5,
                },
            ]
    out = analyse(fs, res, {"fills": fills})
    assert out["markets"] == 72
    allf = out["all_firings"]
    assert (allf["calibration_closes"], allf["evaluation_closes"]) == (4, 8)
    assert allf["chosen_margin_cents"] == pytest.approx(0.25)  # ties -> smallest
    assert (
        allf["evaluation"]["policy"]["mean_cents"]
        > allf["evaluation"]["current_rule"]["mean_cents"]
    )
    assert allf["verdict"] == "YES"
    assert out["paper_filled"]["matched_trades"] == 72
    assert out["paper_filled"]["verdict"] == "YES"
    thin = analyse(fs[:6], res, {"fills": []})
    assert thin["all_firings"]["verdict"] == "NOT YET"
    assert thin["paper_filled"]["verdict"] == "NOT YET"
