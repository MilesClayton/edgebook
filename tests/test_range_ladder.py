from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.engine.state import MarketInfo
from edgebook.history import kalshi as kh
from edgebook.recorder.rows import BOOK, MARKETS
from edgebook.research.range_ladder import (
    P,
    RangeLegs,
    Tops,
    analyse,
    basket_payoff,
    baskets,
    book_files,
    candle_pass,
    check_fills,
    check_results,
    fee_per_contract,
    fill_report,
    longshot_band,
    match_ranges,
    outages,
    per_close,
    replicated_result,
    scan_range,
    summarize,
    top_of_book,
)

CLOSE = 1_790_003_600  # seconds
T0 = (CLOSE - 3600) * 1000  # ms, an hour before close
RANGE = "KXBTC-X-B81050"
LO = "KXBTCD-X-T80999.99"
HI = "KXBTCD-X-T81099.99"
LEGS = RangeLegs(RANGE, CLOSE, "between", LO, HI)


def info(ticker: str, series: str, kind: str, floor: float | None, cap: float | None) -> MarketInfo:
    return MarketInfo(ticker, series, CLOSE - 3600, CLOSE, kind, floor, cap)


def ladder(*strikes: float) -> dict[str, MarketInfo]:
    return {
        f"KXBTCD-X-T{k:.2f}": info(f"KXBTCD-X-T{k:.2f}", "KXBTCD", "greater", k, None)
        for k in strikes
    }


def u(x: float) -> int:
    return round(x * P)


def tops(*rows: tuple[int, float, float, float, float]) -> Tops:
    """Rows of (ms after T0, yes bid, yes size, no bid, no size); bid -1 = none."""
    a = np.array(rows, dtype=float)
    return Tops(
        (T0 + a[:, 0]).astype(np.int64),
        np.where(a[:, 1] < 0, -1, np.rint(a[:, 1] * P)).astype(np.int64),
        a[:, 2],
        np.where(a[:, 3] < 0, -1, np.rint(a[:, 3] * P)).astype(np.int64),
        a[:, 4],
    )


def scenario() -> dict[str, Tops]:
    """Range YES ask 30c = ladder price (lower NO 40c + upper YES 30c) until the range's
    NO bid jumps to 78c at +10s (YES ask 22c) and falls back at +25s."""
    return {
        RANGE: tops(
            (0, 0.20, 5, 0.70, 10), (10_000, 0.20, 5, 0.78, 10), (25_000, 0.20, 5, 0.70, 10)
        ),
        LO: tops((0, 0.60, 50, 0.38, 50)),
        HI: tops((0, 0.20, 20, 0.70, 20)),
    }


# ---------------------------------------------------------------- matching


def test_ranges_match_the_ladder_strike_a_cent_below_each_edge() -> None:
    markets = ladder(80999.99, 81099.99, 81199.99)
    markets[RANGE] = info(RANGE, "KXBTC", "between", 81000.0, 81099.99)
    markets["KXBTC-X-T81200"] = info("KXBTC-X-T81200", "KXBTC", "greater", 81199.99, None)
    markets["KXBTC-X-T81000"] = info("KXBTC-X-T81000", "KXBTC", "less", None, 81000.0)
    markets["KXBTC-X-B81250"] = info("KXBTC-X-B81250", "KXBTC", "between", 81200.0, 81299.99)
    # same strikes at a different close never match
    markets["KXBTC-Y-B81050"] = MarketInfo(
        "KXBTC-Y-B81050", "KXBTC", 0, CLOSE + 3600, "between", 81000.0, 81099.99
    )
    matched, unmatched = match_ranges(markets)
    assert {m.ticker: (m.lower, m.upper) for m in matched} == {
        RANGE: (LO, HI),
        "KXBTC-X-T81200": ("KXBTCD-X-T81199.99", None),
        "KXBTC-X-T81000": (None, LO),
    }
    assert unmatched == ["KXBTC-X-B81250", "KXBTC-Y-B81050"]


def test_replicated_result_and_boundary_check() -> None:
    assert replicated_result(LEGS, {LO: "yes", HI: "no"}) == "yes"
    assert replicated_result(LEGS, {LO: "yes", HI: "yes"}) == "no"
    assert replicated_result(LEGS, {LO: "yes"}) is None
    below = RangeLegs("B", CLOSE, "less", None, LO)
    assert replicated_result(below, {LO: "no"}) == "yes"
    above = RangeLegs("A", CLOSE, "greater", HI, None)
    assert replicated_result(above, {HI: "yes"}) == "yes"
    res = {RANGE: "yes", LO: "yes", HI: "no", "B": "yes", "A": "no"}
    assert check_results([LEGS, below, above], res) == {"settled_checked": 3, "mismatches": 1}
    assert check_results([LEGS], {LO: "yes", HI: "no"}) == {"settled_checked": 0, "mismatches": 0}


def test_baskets_pay_the_same_in_every_outcome() -> None:
    b = baskets(LEGS)
    assert b["buy_range"] == ((RANGE, "yes"), (LO, "no"), (HI, "yes"))
    assert b["sell_range"] == ((RANGE, "no"), (LO, "yes"), (HI, "no"))
    for direction, basket in b.items():
        for spot in (80_900.0, 81_050.0, 81_150.0):
            yes = {RANGE: 81_000 <= spot < 81_100, LO: spot > 80_999.99, HI: spot > 81_099.99}
            paid = sum(yes[tk] == (side == "yes") for tk, side in basket)
            assert paid == basket_payoff(LEGS, direction)
    tail = RangeLegs("A", CLOSE, "greater", HI, None)
    assert basket_payoff(tail, "sell_range") == 1
    assert basket_payoff(tail, "buy_range") == 1


def test_fee_matches_core_fees() -> None:
    prices = np.array([u(p) for p in (0.01, 0.22, 0.4, 0.5, 0.97)], dtype=np.int64)
    for lot in (1, 7, 100):
        got = fee_per_contract(prices, np.full(len(prices), lot, dtype=np.int64))
        want = [
            float(trade_fee(lot, Decimal(int(p)) / P, LiquidityRole.TAKER)) / lot for p in prices
        ]
        assert got == pytest.approx(want)


# ---------------------------------------------------------------- books


def book_table(rows: list[dict[str, Any]]) -> pa.Table:
    full = [
        {
            "recv_ts_ms": r["ts"],
            "sending_ts_ms": r["ts"],
            "sid": 1,
            "seq": i,
            "kind": r["kind"],
            "ticker": r["ticker"],
            "side": r.get("side"),
            "price": None if r.get("price") is None else Decimal(str(r["price"])),
            "qty": None if r.get("qty") is None else Decimal(str(r["qty"])),
        }
        for i, r in enumerate(rows)
    ]
    return pa.Table.from_pylist(full, schema=BOOK)


def snapshot(
    ticker: str, ts: int, yes: dict[float, float], no: dict[float, float]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [{"ts": ts, "kind": "reset", "ticker": ticker}]
    for side, lv in (("yes", yes), ("no", no)):
        rows += [
            {"ts": ts, "kind": "level", "ticker": ticker, "side": side, "price": p, "qty": q}
            for p, q in lv.items()
        ]
    return rows


def test_top_of_book_replays_snapshots_deltas_and_outages(tmp_path: Path) -> None:
    rows = snapshot("A", 1000, {0.20: 5, 0.18: 3}, {0.70: 10})
    rows += [
        {"ts": 1100, "kind": "delta", "ticker": "A", "side": "yes", "price": 0.20, "qty": -5},
        {"ts": 1200, "kind": "delta", "ticker": "A", "side": "no", "price": 0.71, "qty": 2.5},
        {"ts": 1300, "kind": "delta", "ticker": "A", "side": "no", "price": 0.50, "qty": -1},
        {"ts": 1400, "kind": "delta", "ticker": "A", "side": "no", "price": 0.72, "qty": 1},
        {"ts": 1500, "kind": "delta", "ticker": "Z", "side": "no", "price": 0.72, "qty": 1},
    ]
    rows += snapshot("A", 1600, {0.30: 1}, {0.60: 2})
    rows += [{"ts": 9000, "kind": "delta", "ticker": "A", "side": "yes", "price": 0.31, "qty": 4}]
    rows += snapshot("A", 9100, {0.32: 1}, {0.60: 2})
    (tmp_path / "book" / "date=x").mkdir(parents=True)
    pq.write_table(book_table(rows[:9]), tmp_path / "book" / "date=x" / "a.parquet")
    pq.write_table(book_table(rows[9:]), tmp_path / "book" / "date=x" / "b.parquet")
    (tmp_path / "book" / "date=x" / "torn.parquet").write_bytes(b"PAR1junk")
    (tmp_path / "book" / "date=x" / "empty.parquet").write_bytes(b"")
    files = book_files(tmp_path)
    assert len(files) == 3
    gaps = outages(files)
    assert gaps == [(1600, 9000)]
    t = top_of_book(files, {"A"}, gaps)["A"]
    got = list(
        zip(
            t.ts.tolist(),
            t.yes_bid.tolist(),
            t.yes_size.tolist(),
            t.no_bid.tolist(),
            t.no_size.tolist(),
            strict=True,
        )
    )
    assert got == [
        (1000, u(0.20), 5.0, u(0.70), 10.0),
        (1100, u(0.18), 3.0, u(0.70), 10.0),  # best YES level emptied
        (1200, u(0.18), 3.0, u(0.71), 2.5),
        (1300, -1, 0.0, -1, 0.0),  # negative size: book unusable until the next snapshot
        (1600, u(0.30), 1.0, u(0.60), 2.0),
        (9000, -1, 0.0, -1, 0.0),  # first message after an outage: stale until a snapshot
        (9100, u(0.32), 1.0, u(0.60), 2.0),
    ]
    assert "Z" not in top_of_book(files, {"A"}, gaps)


# ---------------------------------------------------------------- scan


def test_scan_finds_the_fee_surviving_gap_and_how_long_it_lasts() -> None:
    s = scan_range(LEGS, scenario(), end_ms=T0 + 60_000)
    assert s is not None
    assert s.quoted_ms == 60_000
    assert s.prefee_ms == {"buy_range": 15_000, "sell_range": 0}
    (e,) = s.episodes
    assert (e.direction, e.start_ms, e.duration_ms, e.lot) == ("buy_range", T0 + 10_000, 15_000, 10)
    # cost 22 + 40 + 30 = 92c; fees on a 10-lot: 1.3 + 1.7 + 1.5 = 4.5c at whole cents
    fees = sum(
        float(trade_fee(10, Decimal(px), LiquidityRole.TAKER)) / 10
        for px in ("0.22", "0.40", "0.30")
    )
    assert 0.0435 <= fees <= 0.045  # exact 4.352c at $0.0001 rounding
    assert e.max_edge == pytest.approx(0.08 - fees)
    assert e.dollars == pytest.approx(10 * (0.08 - fees))
    assert e.prices == pytest.approx([0.22, 0.40, 0.30])
    assert e.secs_to_close == pytest.approx(3590)
    assert s.best_prefee["sell_range"] == pytest.approx(-0.22)


def test_scan_drops_book_states_left_over_from_an_outage() -> None:
    gaps = [(T0 + 15_000, T0 + 20_000)]
    s = scan_range(LEGS, scenario(), end_ms=T0 + 60_000, gaps=gaps)
    assert s is not None
    (e,) = s.episodes
    assert e.duration_ms == 5_001  # ends 1 ms after the outage starts
    # the ladder legs never sent a book after the outage: nothing after it counts
    assert s.quoted_ms == 15_001


def test_scan_needs_every_leg_two_sided_and_recorded() -> None:
    books = scenario()
    assert scan_range(LEGS, {RANGE: books[RANGE], LO: books[LO]}, end_ms=T0 + 60_000) is None
    assert scan_range(LEGS, books, end_ms=T0 - 1) is None
    books[HI] = tops((0, -1, 0, 0.70, 20))
    s = scan_range(LEGS, books, end_ms=T0 + 60_000)
    assert s is not None
    assert s.quoted_ms == 0
    assert s.episodes == []
    assert np.isnan(s.best_edge["buy_range"])


def test_tail_has_a_single_ladder_leg() -> None:
    tail = RangeLegs("KXBTC-X-T81100", CLOSE, "greater", HI, None)
    books = {tail.ticker: tops((0, 0.10, 5, 0.70, 5)), HI: tops((0, 0.25, 5, 0.70, 5))}
    s = scan_range(tail, books, end_ms=T0 + 1000)
    assert s is not None
    # identical contracts: buy the tail at 30c and the ladder's NO at 75c = $1.05 for $1
    assert s.best_prefee["buy_range"] == pytest.approx(-0.05)
    # sell: tail NO 90c + ladder YES 30c
    assert s.best_prefee["sell_range"] == pytest.approx(-0.20)


def test_reports_per_close_and_summary() -> None:
    s = scan_range(LEGS, scenario(), end_ms=T0 + 60_000)
    assert s is not None
    quiet = scan_range(
        RangeLegs("KXBTC-Y-B81050", CLOSE + 3600, "between", LO, HI),
        {**scenario(), "KXBTC-Y-B81050": tops((0, 0.20, 5, 0.70, 10))},
        end_ms=T0 + 60_000,
    )
    assert quiet is not None
    rows = per_close([s, quiet])
    assert [r["episodes"] for r in rows] == [1, 0]
    assert rows[0]["gap_seconds"] == pytest.approx(15)
    assert rows[0]["prefee_gap_share"] == pytest.approx(0.25)
    assert per_close([s], min_lot=20)[0]["episodes"] == 0
    out = summarize([s, quiet])
    assert out["closes"] == 2
    assert out["by_direction"]["buy_range"]["episodes"] == 1
    assert out["per_close[all]"]["closes_with_gap"] == 1
    assert out["per_close[lasting>=1s,lot>=10]"]["episodes_per_close"]["mean"] == pytest.approx(0.5)
    assert out["by_direction"]["sell_range"]["duration_ms"] == {}
    assert np.isnan(summarize([])["prefee_gap_share_per_close"]["mean"])


# ---------------------------------------------------------------- longshot fills


def test_fill_check_splits_pnl_into_price_and_outcome() -> None:
    books = scenario()
    fills = [
        {
            "action": "BUY",
            "ticker": RANGE,
            "ts_ms": T0 + 12_000,
            "side": "yes",
            "contracts": 10,
            "price": 0.22,
        },
        {
            "action": "BUY",
            "ticker": RANGE,
            "ts_ms": T0 + 30_000,
            "side": "no",
            "contracts": 30,
            "price": 0.80,
        },
        {
            "action": "SETTLE",
            "ticker": RANGE,
            "ts_ms": T0 + 99_000,
            "side": "yes",
            "contracts": 10,
            "price": 1.0,
        },
        {
            "action": "BUY",
            "ticker": "KXBTCD-X-T1",
            "ts_ms": T0,
            "side": "yes",
            "contracts": 1,
            "price": 0.5,
        },
    ]
    checks = check_fills(fills, {RANGE: LEGS}, books, {RANGE: "yes"})
    assert len(checks) == 2
    yes, no = checks
    # ladder YES mid = lower mid 61c - upper mid 25c = 36c; bid 60 - 30 = 30c, ask 62 - 20 = 42c
    assert (yes.ladder_mid, yes.ladder_bid, yes.ladder_ask) == pytest.approx((0.36, 0.30, 0.42))
    assert yes.range_mid == pytest.approx((0.20 + 0.22) / 2)
    assert (no.ladder_mid, no.ladder_bid, no.ladder_ask) == pytest.approx((0.64, 0.58, 0.70))
    assert (yes.won, no.won) == (True, False)
    rep = fill_report(checks)
    assert rep["fills"] == 2
    assert rep["fills_above_ladder_ask"] == 1  # the NO at 80c, ladder offered it at 70c
    assert rep["fills_inside_ladder_spread"] == 0
    w = (10 * (0.36 - 0.22) + 30 * (0.64 - 0.80)) / 40
    assert rep["price_vs_ladder"]["weighted_cents"] == pytest.approx(100 * w)
    pnl = (10 * 0.78 + 30 * -0.80) / 40
    assert rep["pnl"]["weighted_cents"] == pytest.approx(100 * pnl)
    assert fill_report([]) == {"fills": 0}
    # an unrecorded ladder leg leaves no ladder price
    (blind,) = check_fills(fills[:1], {RANGE: LEGS}, {RANGE: books[RANGE]}, {})
    assert blind.ladder_mid is None
    assert blind.won is None


# ---------------------------------------------------------------- candles


def write_history(d: Path) -> None:
    def market(
        series: str, ticker: str, kind: str, floor: float | None, cap: float | None, res: str
    ) -> dict[str, Any]:
        return {
            "series": series,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "ticker": ticker,
            "open_ts": CLOSE - 3600,
            "close_ts": CLOSE,
            "strike_type": kind,
            "floor": floor,
            "cap": cap,
            "result": res,
            "expiration_value": 81050.0,
        }

    ranges = [market("KXBTC", RANGE, "between", 81000.0, 81099.99, "yes")]
    lad = [
        market("KXBTCD", LO, "greater", 80999.99, None, "yes"),
        market("KXBTCD", HI, "greater", 81099.99, None, "no"),
    ]

    def candle(tk: str, ts: int, bid: float, ask: float) -> dict[str, Any]:
        return {"ticker": tk, "end_ts": ts, "bid": bid, "ask": ask, "last": None, "volume": 0.0}

    m1, m2 = CLOSE - 120, CLOSE - 60
    rc = [
        candle(RANGE, m1, 0.20, 0.30),
        candle(RANGE, m2, 0.20, 0.10),
        candle(RANGE, CLOSE, 0.1, 0.12),
    ]
    lc = [
        candle(LO, m1, 0.60, 0.62),
        candle(LO, m2, 0.60, 0.62),
        candle(LO, CLOSE, 0.6, 0.62),
        candle(HI, m1, 0.20, 0.30),
        candle(HI, m2, 0.20, 0.30),
        candle(HI, CLOSE, 0.2, 0.3),
    ]
    rc.append(candle(RANGE, m1 - 60, 0.20, 0.10))  # crossed: dropped
    d.mkdir(parents=True, exist_ok=True)
    for s, ms, cs in (("KXBTC", ranges, rc), ("KXBTCD", lad, lc)):
        pq.write_table(pa.Table.from_pylist(ms, schema=kh.MARKETS), kh.markets_path(d, s))
        pq.write_table(pa.Table.from_pylist(cs, schema=kh.CANDLES), kh.candles_path(d, s))


def test_candle_pass_flags_minutes_where_the_basket_is_cheap(tmp_path: Path) -> None:
    write_history(tmp_path)
    # make the second minute a real gap: range ask 10c (bid 5c)
    t = pq.read_table(kh.candles_path(tmp_path, "KXBTC")).to_pylist()
    for c in t:
        if c["end_ts"] == CLOSE - 60:
            c["bid"] = 0.05
    pq.write_table(pa.Table.from_pylist(t, schema=kh.CANDLES), kh.candles_path(tmp_path, "KXBTC"))
    out = candle_pass(tmp_path)
    assert out["ranges_matched"] == 1
    assert out["boundary_check"] == {"settled_checked": 1, "mismatches": 0}
    assert out["range_minutes"] == 2  # the close-minute candle is excluded
    assert out["buy_range"]["minutes_with_gap"] == 1
    assert out["sell_range"]["minutes_with_gap"] == 0
    assert out["longshot_band"] == {"minutes": 0}  # both minutes are inside the last 15
    # 10c + 40c + 30c = 80c, minus 100-lot fees (0.63 + 1.68 + 1.47c)
    assert out["buy_range"]["edge_cents"]["p50"] == pytest.approx(20 - 0.63 - 1.68 - 1.47)


# ---------------------------------------------------------------- end to end


def test_analyse_reads_a_recorder_folder(tmp_path: Path) -> None:
    data = tmp_path / "data"
    close = datetime.fromtimestamp(CLOSE, UTC)
    mrows = [
        (RANGE, "KXBTC", "between", "81000.00", "81099.99", "yes"),
        (LO, "KXBTCD", "greater", "80999.99", None, "yes"),
        (HI, "KXBTCD", "greater", "81099.99", None, "no"),
        ("KXBTC-X-B99950", "KXBTC", "between", "99900.00", "99999.99", "no"),
    ]
    (data / "markets").mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "seen_ts_ms": T0,
                    "series": s,
                    "ticker": tk,
                    "event_ticker": tk.rsplit("-", 1)[0],
                    "status": "settled",
                    "close_time": close,
                    "strike_type": k,
                    "floor_strike": None if f is None else Decimal(f),
                    "cap_strike": None if c is None else Decimal(c),
                    "result": res,
                    "expiration_value": "81050",
                }
                for tk, s, k, f, c, res in mrows
            ],
            schema=MARKETS,
        ),
        data / "markets" / "part.parquet",
    )
    rows = snapshot(RANGE, T0, {0.20: 5}, {0.70: 10})
    rows += snapshot(LO, T0, {0.60: 50}, {0.38: 50})
    rows += snapshot(HI, T0, {0.20: 20}, {0.70: 20})
    rows += [
        {
            "ts": T0 + 10_000,
            "kind": "delta",
            "ticker": RANGE,
            "side": "no",
            "price": 0.78,
            "qty": 10,
        },
        {
            "ts": T0 + 25_000,
            "kind": "delta",
            "ticker": RANGE,
            "side": "no",
            "price": 0.78,
            "qty": -10,
        },
        {"ts": T0 + 30_000, "kind": "delta", "ticker": LO, "side": "yes", "price": 0.59, "qty": 1},
    ]
    # other tickers keep the stream alive, so no silence reads as a recorder outage
    rows += [
        {"ts": T0 + ms, "kind": "delta", "ticker": "OTHER", "side": "yes", "price": 0.5, "qty": 1}
        for ms in range(1000, 30_000, 2000)
    ]
    rows.sort(key=lambda r: r["ts"])
    (data / "book").mkdir()
    pq.write_table(book_table(rows), data / "book" / "part.parquet")
    paper = tmp_path / "paper.json"
    paper.write_text(
        json.dumps(
            {
                "fills": [
                    {
                        "action": "BUY",
                        "ticker": RANGE,
                        "ts_ms": T0 + 12_000,
                        "side": "yes",
                        "contracts": 10,
                        "price": 0.22,
                    },
                    {
                        "action": "SETTLE",
                        "ticker": RANGE,
                        "ts_ms": T0 + 99_000,
                        "side": "yes",
                        "contracts": 10,
                        "price": 1.0,
                        "pnl": 7.8,
                    },
                ]
            }
        )
    )
    hist = tmp_path / "history"
    write_history(hist)
    out = analyse(data, paper, hist)
    assert out["ranges_matched"] == 1
    assert out["ranges_unmatched"] == 1
    assert out["boundary_check"] == {"settled_checked": 1, "mismatches": 0}
    assert out["books"]["episodes"] == 1
    assert out["top_episodes"][0]["duration_ms"] == 15_000
    assert out["longshot_fills"]["fills"] == 1
    assert out["longshot_fills"]["account_settled"]["weighted_cents"] == pytest.approx(78)
    assert out["candles"]["ranges_matched"] == 1


def test_longshot_band_compares_ask_ladder_and_outcome() -> None:
    rows = [(CLOSE, 0.05, 0.08, 1.0), (CLOSE, 0.10, 0.08, 0.0), (CLOSE + 3600, 0.03, 0.02, 0.0)]
    out = longshot_band(rows)
    assert (out["minutes"], out["closes"]) == (3, 2)
    assert out["ladder_mid_minus_ask"]["cents"] == pytest.approx(100 * (0.03 - 0.02 - 0.01) / 3)
    assert out["won_minus_ask"]["cents"] == pytest.approx(100 * (0.95 - 0.10 - 0.03) / 3)
    assert out["won_minus_ladder_mid"]["cents"] == pytest.approx(100 * (0.92 - 0.08 - 0.02) / 3)
