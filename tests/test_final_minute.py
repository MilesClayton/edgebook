from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.recorder.rows import SCHEMAS
from edgebook.research.final_minute import (
    MIN_VOL_PER_S,
    Contract,
    Entry,
    Execution,
    Ladder,
    Prints,
    Signal,
    analyse,
    bar,
    book_snapshots,
    build_signals,
    choose_threshold,
    entries,
    execute_buy,
    fee_per_contract,
    load_contracts,
    load_prints,
    nowcast,
    parquet_files,
    realized_sigma,
    scorecard,
    settlement_average,
    settlement_check,
    settles_yes,
    span_days,
    split_closes,
    taker_fee,
)

T0 = 1_790_000_000_000  # whole minute, ms


def prints_from(values: list[float], start_ms: int = T0, lag_ms: int = 400) -> Prints:
    return Prints.from_rows(
        (start_ms + i * 1000, v, start_ms + i * 1000 + 200, start_ms + i * 1000 + lag_ms)
        for i, v in enumerate(values)
    )


def write(root: Path, stream: str, rows: list[dict[str, object]], first_ms: int, seq: int) -> None:
    out = root / stream / "date=2026-10-17"
    out.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=SCHEMAS[stream])
    pq.write_table(table, out / f"part-{first_ms}-{seq}.parquet")


def book_row(
    recv: int, kind: str, ticker: str, side: str | None, price: str | None, qty: str | None
) -> dict[str, object]:
    return {
        "recv_ts_ms": recv,
        "sending_ts_ms": recv - 30,
        "sid": 1,
        "seq": 1,
        "kind": kind,
        "ticker": ticker,
        "side": side,
        "price": Decimal(price) if price else None,
        "qty": Decimal(qty) if qty else None,
    }


# ------------------------------------------------------------------ settlement and nowcast


def test_prints_keep_first_receipt_and_sort() -> None:
    p = Prints.from_rows(
        [(2000, 2.0, 2100, 2300), (1000, 1.0, 1100, 1500), (1000, 9.0, 1100, 1200)]
    )
    assert p.time_ms.tolist() == [1000, 2000]
    assert p.value.tolist() == [9.0, 2.0]  # the earlier receipt wins


def test_settlement_average_uses_half_open_window() -> None:
    close = T0 + 120_000
    vals = [100.0] * 60 + [200.0] * 60 + [999.0]  # print at `close` must be excluded
    p = prints_from(vals)
    avg, n = settlement_average(p, close)
    assert (avg, n) == (200.0, 60)
    assert math.isnan(settlement_average(p, T0 - 1)[0])


def test_settles_yes_respects_strike_type() -> None:
    assert settles_yes(100.0, 100.0, "greater_or_equal")
    assert not settles_yes(100.0, 100.0, "greater")
    assert settles_yes(100.01, 100.0, "greater")


def test_realized_sigma_floor_and_estimate() -> None:
    flat = prints_from([100.0] * 100)
    assert realized_sigma(flat, 100) == MIN_VOL_PER_S
    assert realized_sigma(flat, 10) == MIN_VOL_PER_S  # too few prints
    rng = np.random.default_rng(1)
    walk = 100 * np.exp(np.cumsum(rng.normal(0, 1e-3, 900)))
    s = realized_sigma(prints_from(walk.tolist()), 900)
    assert s == pytest.approx(1e-3, rel=0.1)
    gappy = Prints.from_rows((T0 + i * 2000, 100.0 + i, T0, T0) for i in range(100))
    assert realized_sigma(gappy, 100) == MIN_VOL_PER_S  # no consecutive 1 s steps


def test_nowcast_known_part_and_remaining_variance() -> None:
    close = T0 + 960_000
    vals = [100.0] * 900 + [110.0] * 30  # 30 window prints known at 110
    p = prints_from(vals)
    nc = nowcast(p, close, close - 30_000 + 400)  # print at close-31s received
    assert nc is not None
    assert nc.known_n == 30
    assert nc.known_avg == pytest.approx(110.0)
    assert nc.remaining_n == 30
    assert nc.mean == pytest.approx(110.0)
    n = 30
    expected = 110.0 * nc.sigma * math.sqrt(n * (n + 1) * (2 * n + 1) / 6) / 60
    assert nc.sd == pytest.approx(expected)
    assert nc.prob_yes(nc.mean) == pytest.approx(0.5)
    assert nc.prob_yes(nc.mean - 3 * nc.sd) > 0.99
    assert nc.prob_yes(nc.mean - 3 * nc.sd, nu=4) < nc.prob_yes(nc.mean - 3 * nc.sd)


def test_nowcast_before_window_and_degenerate_cases() -> None:
    close = T0 + 960_000
    p = prints_from([100.0] * 960)
    early = nowcast(p, close, close - 70_000)
    assert early is not None
    assert early.known_n == 0
    assert math.isnan(early.known_avg)
    assert early.remaining_n == 60
    assert nowcast(p, close, T0 - 1) is None  # nothing received yet
    assert nowcast(p, close + 60_000, close + 30_000) is None  # feed stalled > 10 s
    done = nowcast(p, close, close + 5_000)
    assert done is not None
    assert done.remaining_n == 0
    assert done.sd == 0
    assert done.prob_yes(100.0) == 1.0
    assert done.prob_yes(100.01) == 0.0
    past = prints_from([100.0] * 30, start_ms=close + 60_000)
    assert nowcast(past, close, close + 61_000) is None  # no window prints, nothing left


# ------------------------------------------------------------------ book and execution


LAD = Ladder(yes=((0.40, 5.0), (0.39, 10.0)), no=((0.55, 3.0), (0.54, 4.0), (0.50, 100.0)))


def test_ladder_quotes() -> None:
    assert LAD.yes_bid == 0.40
    assert LAD.yes_ask == 0.45
    assert LAD.ask("yes") == (0.45, 3.0)
    assert LAD.ask("no") == (0.60, 5.0)
    assert LAD.depth_at("yes", 0.46) == 7.0
    assert not LAD.crossed
    empty = Ladder(yes=(), no=())
    assert empty.yes_bid is None
    assert empty.yes_ask is None
    assert empty.ask("yes") is None
    assert not empty.crossed
    assert Ladder(yes=((0.6, 1.0),), no=((0.5, 1.0),)).crossed


def test_execute_buy_walks_depth_to_limit() -> None:
    ex = execute_buy(LAD, "yes", 0.46, 10)
    assert ex.filled == 7.0
    assert ex.avg_price == pytest.approx((3 * 0.45 + 4 * 0.46) / 7)
    assert ex.fees == pytest.approx(taker_fee(0.45, 3) + taker_fee(0.46, 4))
    assert execute_buy(LAD, "yes", 0.44, 10).filled == 0
    assert execute_buy(None, "yes", 0.5, 1).filled == 0
    tiny = Ladder(yes=(), no=((0.55, 0.004), (0.54, 2.0)))
    assert execute_buy(tiny, "yes", 0.46, 1).filled == 1.0  # sub-cent level skipped


def test_fees() -> None:
    # core.fees rounds the exact 1.75c up to the account's balance precision
    assert taker_fee(0.50, 1) == float(trade_fee(1, Decimal("0.50"), LiquidityRole.TAKER))
    assert 0.0175 <= taker_fee(0.50, 1) <= 0.02
    assert fee_per_contract(0.50) == pytest.approx(0.0175)


def test_book_snapshots_replay_resets_and_deltas(tmp_path: Path) -> None:
    t = "KXBTCD-X"
    rows = [
        book_row(T0 + 10, "level", t, "yes", "0.30", "9"),  # before any reset: ignored
        book_row(T0 + 100, "reset", t, None, None, None),
        book_row(T0 + 100, "level", t, "yes", "0.40", "5"),
        book_row(T0 + 100, "level", t, "no", "0.55", "3"),
        book_row(T0 + 500, "delta", t, "no", "0.55", "-1"),
        book_row(T0 + 600, "level", "OTHER", "no", "0.5", "1"),
    ]
    write(tmp_path, "book", rows, T0, 0)
    write(tmp_path, "book", [book_row(T0 + 2000, "delta", t, "yes", "0.40", "1")], T0 + 2000, 1)
    (tmp_path / "book" / "date=2026-10-17" / "part-1-9.parquet").touch()  # empty file skipped
    snaps = book_snapshots(tmp_path, {t: [T0 + 50, T0 + 200, T0 + 500, T0 + 1500, T0 + 2500]})
    assert (t, T0 + 50) not in snaps  # not ready before the first reset
    assert snaps[(t, T0 + 200)].ask("yes") == (0.45, 3.0)
    assert snaps[(t, T0 + 500)].ask("yes") == (0.45, 2.0)
    assert snaps[(t, T0 + 1500)].yes == ((0.40, 5.0),)
    assert (t, T0 + 2500) not in snaps  # after the last recorded row: no claim


def test_book_snapshots_resets_within_one_batch(tmp_path: Path) -> None:
    t = "KXBTCD-Y"
    rows = [
        book_row(T0, "reset", t, None, None, None),
        book_row(T0, "level", t, "no", "0.50", "8"),
        book_row(T0 + 5, "delta", t, "no", "0.50", "-2"),
        book_row(T0 + 10, "reset", t, None, None, None),
        book_row(T0 + 10, "level", t, "no", "0.52", "1"),
        book_row(T0 + 50, "delta", t, "no", "0.52", "1"),
        book_row(T0 + 99, "delta", t, "no", "0.52", "1"),
    ]
    write(tmp_path, "book", rows, T0, 0)
    snaps = book_snapshots(tmp_path, {t: [T0 + 20, T0 + 60]})
    assert snaps[(t, T0 + 20)].no == ((0.52, 1.0),)
    assert snaps[(t, T0 + 60)].no == ((0.52, 2.0),)
    assert parquet_files(tmp_path, "book")[0].name == f"part-{T0}-0.parquet"


# ------------------------------------------------------------------ experiment


def contract(
    ticker: str, close_ms: int, strike: float, result: str, kind: str = "greater_or_equal"
) -> Contract:
    return Contract(ticker, "KXBTCD", close_ms, strike, kind, result, None)


def signal(
    ticker: str, checkpoint: int, edge: float, won_yes: bool, ex: Execution, close: int = T0
) -> Signal:
    return Signal(
        ticker=ticker,
        series="KXBTCD",
        close_ms=close,
        checkpoint_s=checkpoint,
        p_yes=0.9,
        won_yes=won_yes,
        yes_bid=0.5,
        yes_ask=0.6,
        side="yes",
        edge=edge,
        limit=0.6,
        shown=5,
        executions={0: ex},
    )


def test_entries_take_the_earliest_qualifying_checkpoint() -> None:
    ex = Execution(2.0, 0.6, 0.04)
    sigs = [
        signal("A", 30, 0.05, True, ex),
        signal("A", 45, 0.05, True, Execution(1.0, 0.7, 0.01)),
        signal("A", 60, 0.01, True, ex),  # below threshold
        signal("B", 15, 0.05, False, Execution(0.0, math.nan, 0.0)),
    ]
    rows = {e.ticker: e for e in entries(sigs, 0.03, 0)}
    assert rows["A"].checkpoint_s == 45
    assert rows["A"].pnl_per_contract == pytest.approx(1 - 0.7 - 0.01)
    assert rows["B"].filled == 0
    assert math.isnan(rows["B"].pnl_per_contract)


def test_bar_reports_close_level_numbers() -> None:
    rows = [
        Entry("A", T0, 30, "yes", 0.1, 2, 0.3),
        Entry("B", T0, 30, "yes", 0.1, 4, -0.5),
        Entry("C", T0 + 60_000, 30, "no", 0.1, 1, 0.2),
        Entry("D", T0 + 60_000, 30, "no", 0.1, 0, math.nan),
    ]
    b = bar(rows, 0.5)
    assert b["attempts"] == 4
    assert b["filled"] == 3
    assert b["closes"] == 2
    assert b["fill_rate"] == 0.75
    assert b["mean_cents"] == pytest.approx(0.0)
    assert b["trades_per_day"] == 6
    assert b["worst_close_dollars"] == pytest.approx(-0.2)
    assert b["contracts_per_fill"] == pytest.approx(7 / 3)
    empty = bar([], 0)
    assert empty["mean_cents"] is None
    assert empty["trades_per_day"] is None
    assert empty["worst_close_dollars"] is None
    assert empty["fill_rate"] is None


def test_split_threshold_and_span() -> None:
    win, lose = Execution(1.0, 0.6, 0.02), Execution(1.0, 0.6, 0.02)
    sigs = [signal(f"W{i}", 30, 0.08, True, win, T0 + i * 60_000) for i in range(25)]
    sigs += [signal(f"L{i}", 30, 0.02, False, lose, T0 + i * 60_000) for i in range(25)]
    train, test = split_closes(sigs)
    assert len(train) == 8
    assert len(test) == 17
    assert not train & test
    th, table = choose_threshold(sigs, min_fills=20)
    assert 0.02 < th <= 0.08  # drops the losers, keeps the winners
    assert table[0]["fills"] == 50
    assert span_days([T0, T0 + 86_400_000]) == 1.0
    assert span_days([T0]) == 0.0


def test_build_signals_and_scorecard() -> None:
    close = T0 + 960_000
    p = prints_from([100.0] * 900 + [110.0] * 30)
    lad = Ladder(yes=((0.50, 10.0),), no=((0.40, 10.0),))  # yes ask 0.60, no ask 0.50
    contracts = {
        "UP": contract("UP", close, 100.0, "yes"),  # nowcast ~certain yes
        "DN": contract("DN", close, 120.0, "no", "greater"),  # ~certain no
        "NOBOOK": contract("NOBOOK", close, 100.0, "yes"),
    }
    d = close - 30_000
    ladders = {(k, d + dl): lad for k in ("UP", "DN") for dl in (0, 100)}
    ladders[("UP", d + 100)] = Ladder(yes=(), no=())  # book pulled before arrival
    sigs = build_signals(contracts, p, ladders, checkpoints=(30,), delays=(0, 100))
    by = {s.ticker: s for s in sigs}
    assert set(by) == {"UP", "DN"}
    assert by["UP"].side == "yes"
    assert by["UP"].edge == pytest.approx(1 - 0.6 - fee_per_contract(0.6))
    assert by["DN"].side == "no"
    assert by["UP"].executions[0].filled == 10
    assert by["UP"].executions[100].filled == 0
    card = scorecard(sigs)
    assert card[0]["checkpoint_s"] == 30
    assert card[0]["markets"] == 2
    assert card[0]["brier_nowcast"] < card[0]["brier_kalshi_mid"]
    assert card[0]["mean_value_at_ask_cents"] > 0
    one_sided = {(k, d): Ladder(yes=(), no=()) for k in ("UP",)}
    assert (
        build_signals({"UP": contracts["UP"]}, p, one_sided, checkpoints=(30,), delays=(0,)) == []
    )
    losing = {("UP", d): Ladder(yes=((0.98, 1.0),), no=((0.01, 1.0),))}
    sig = build_signals({"UP": contracts["UP"]}, p, losing, checkpoints=(30,), delays=(0,))
    assert len(sig) == 1
    assert scorecard(sig) == []


def test_settlement_check_counts_agreements() -> None:
    close = T0 + 120_000
    p = prints_from([100.0] * 60 + [105.0] * 60)
    cs = {
        "A": Contract("A", "KXBTCD", close, 104.0, "greater", "yes", 105.0),
        "B": Contract("B", "KXBTCD", close, 106.0, "greater", "yes", 105.2),
        "C": Contract("C", "KXBTCD", T0 - 60_000, 106.0, "greater", "yes", None),
    }
    out = settlement_check(cs, p)
    assert (out["agree"], out["disagree"], out["missing_window"]) == (1, 1, 1)
    assert out["max_abs_gap_to_expiration_value"] == pytest.approx(0.2)


# ------------------------------------------------------------------ end to end


def build_root(root: Path) -> None:
    rng = np.random.default_rng(7)
    n = 2400
    vals = 100_000 * np.exp(np.cumsum(rng.normal(0, 2e-4, n)))
    index_rows = []
    for i, v in enumerate(vals):
        t = T0 + i * 1000
        raw = json.dumps({"type": "value", "id": "BRTI", "time": t, "value": f"{v:.2f}"})
        row = {
            "recv_ts_ms": t + 450,
            "received_at_ms": t + 300,
            "index_id": "BRTI",
            "avg_60s": None,
            "window_start_ms": None,
            "window_end_ms": None,
            "raw": raw,
        }
        index_rows.append(row)
    index_rows.append({**index_rows[0], "index_id": "ETHUSD_RTI"})
    write(root, "index", index_rows, T0, 0)

    settle, book = [], []
    p = prints_from(vals.tolist())
    for k, close in enumerate(range(T0 + 1_020_000, T0 + n * 1000, 60_000)):
        avg, _ = settlement_average(p, close)
        for j, off in enumerate((-30.0, 0.0, 30.0)):
            strike = round(float(p.value[int((close - T0) / 1000) - 61]) + off, 2)
            ticker = f"KXBTCD-{k}-{j}"
            settle.append(
                {
                    "seen_ts_ms": close + 5000,
                    "series": "KXBTCD",
                    "ticker": ticker,
                    "event_ticker": f"E{k}",
                    "status": "settled",
                    "close_time": datetime.fromtimestamp(close / 1000, tz=UTC),
                    "strike_type": "greater",
                    "floor_strike": Decimal(f"{strike:.2f}"),
                    "cap_strike": None,
                    "result": "yes" if avg > strike else "no",
                    "expiration_value": f"{avg:.2f}",
                }
            )
            first = close - 61_000
            book.append(book_row(first, "reset", ticker, None, None, None))
            book.append(book_row(first, "level", ticker, "yes", "0.45", "20"))
            book.append(book_row(first, "level", ticker, "no", "0.45", "20"))
            book.append(book_row(close - 10_000, "delta", ticker, "no", "0.45", "-5"))
    settle.append({**settle[0], "ticker": "BAND", "strike_type": "between"})
    settle.append({**settle[0], "ticker": "OTHER", "series": "KXETHD"})
    settle.append({**settle[0], "ticker": "VOID", "result": ""})
    settle.append({**settle[0], "ticker": "NOEV", "expiration_value": None})
    write(root, "settlements", settle, T0, 0)
    write(root, "book", book, T0, 0)


def test_loaders_and_analyse_end_to_end(tmp_path: Path) -> None:
    build_root(tmp_path)
    cs = load_contracts(tmp_path)
    assert "BAND" not in cs
    assert "OTHER" not in cs
    assert "VOID" not in cs
    assert cs["NOEV"].expiration_value is None
    assert load_prints(tmp_path).value.shape == (2400,)
    out = analyse(tmp_path)
    assert out["settlement_check"]["disagree"] == 0
    assert out["data"]["contracts"] > 0
    assert out["data"]["signals"] > 0
    assert set(out["delay_curve_test"]) == {0, 100, 300, 500, 1000, 2000}
    lat = out["latency_ms"]
    assert lat["brti_print_to_recorder"]["p50"] == 450
    assert lat["brti_kalshi_to_recorder"]["p50"] == 150
    assert lat["book_kalshi_send_to_recorder"]["p50"] == 30
    with pytest.raises(FileNotFoundError):
        load_contracts(tmp_path / "missing")
