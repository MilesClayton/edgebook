from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from edgebook.engine.maker import LongshotConfig
from edgebook.research.longshot_gaps import (
    FEATURES,
    STUDY_CONFIG,
    Firing,
    _filled,
    _long_shot,
    chrono_split,
    event_ci,
    firings,
    first_per_market,
    gap_table,
    seller_pnl,
    summary,
)

CFG = LongshotConfig()
T0 = 1_790_000_000


def firing(**kw: object) -> Firing:
    base: dict[str, object] = {
        "event": "E1",
        "ticker": "E1-T1",
        "close_ts": T0,
        "minutes_left": 30,
        "long_ask": 0.05,
        "won_long": False,
        "filled": True,
        "pnl": seller_pnl(0.05, False),
        "features": {"minutes_left": 30.0, "hourly": 1.0},
    }
    base.update(kw)
    return Firing(**base)  # type: ignore[arg-type]


def test_seller_pnl_is_kept_premium_minus_maker_fee() -> None:
    # sell a 5c long shot = buy the favourite at 0.95; BTC series charge makers nothing
    assert seller_pnl(0.05, False) == pytest.approx(0.05)
    assert seller_pnl(0.05, True) == pytest.approx(-0.95)


def test_long_shot_side_follows_the_paper_rule() -> None:
    assert _long_shot(0.04, 0.05, CFG) == (True, 0.05)  # UP offered at 5c
    assert _long_shot(0.94, 0.97, CFG) == (False, 0.06)  # DOWN at 6c (rounded to 4 dp)
    assert _long_shot(0.40, 0.45, CFG) is None


def test_fill_needs_a_trade_or_quote_at_our_price_within_the_window() -> None:
    rows = [
        (T0, 0.04, 0.05, 0.04, 10.0),
        (T0 + 60, 0.04, 0.05, 0.04, 10.0),  # traded below our 5c ask: no
        (T0 + 120, 0.04, 0.06, 0.05, 0.0),  # "last" at 5c but no volume: no
        (T0 + 900, 0.05, 0.06, 0.05, 3.0),  # too late
    ]
    assert not _filled(rows, 0, True, 0.05)
    rows[2] = (T0 + 120, 0.04, 0.06, 0.05, 2.0)
    assert _filled(rows, 0, True, 0.05)
    # selling DOWN at 6c rests a YES bid at 0.94; filled when YES trades at or below it
    down = [(T0, 0.94, 0.97, 0.95, 1.0), (T0 + 60, 0.93, 0.94, 0.95, 1.0)]
    assert _filled(down, 0, False, 0.06)


def test_one_entry_per_market_takes_the_earliest_filled_firing() -> None:
    rows = [
        firing(minutes_left=30),
        firing(minutes_left=45, filled=False),
        firing(minutes_left=20),
        firing(ticker="E1-T2", minutes_left=50),
    ]
    chosen = first_per_market(rows)
    assert sorted((r.ticker, r.minutes_left) for r in chosen) == [("E1-T1", 30), ("E1-T2", 50)]
    assert first_per_market(rows, np.array([False, True, True, False])) == [rows[2]]


def test_event_bootstrap_and_summary() -> None:
    pnl = np.array([0.04, 0.04, -0.96, 0.04])
    lo, hi = event_ci(pnl, np.array(["a", "a", "b", "c"]))
    assert lo <= float(pnl.mean()) <= hi
    assert np.isnan(event_ci(np.array([]), np.array([]))[0])
    s = summary([firing(), firing(event="E2", won_long=True, pnl=seller_pnl(0.05, True))])
    assert s["n"] == 2
    assert s["closes"] == 2
    assert s["mean_cents"] == pytest.approx(-45.0)


def test_chronological_split_keeps_each_close_in_one_part() -> None:
    rows = [firing(event=f"E{i}", close_ts=T0 + i) for i in range(10) for _ in range(2)]
    tr, va, te = chrono_split(rows)
    events = [{rows[j].event for j in part} for part in (tr, va, te)]
    assert [len(e) for e in events] == [6, 2, 2]
    assert not (events[0] & events[2])
    assert max(rows[j].close_ts for j in tr) < min(rows[j].close_ts for j in te)


def test_gap_table_buckets_filled_firings_only() -> None:
    def feats(minutes: float, hourly: float) -> dict[str, float]:
        return {name: 0.0 for name in FEATURES} | {"minutes_left": minutes, "hourly": hourly}

    rows = [firing(features=feats(10.0, 0.0)) for _ in range(40)]
    rows += [firing(filled=False, features=feats(40.0, 1.0))] * 40
    table = gap_table(rows)
    assert [b["bucket"] for b in table["minutes_left"]] == ["[5, 15)"]
    assert table["hourly"][0]["n"] == 40


def test_firings_rebuild_the_rule_from_a_tiny_history(tmp_path: Path) -> None:
    close = T0 + 3600
    spot_ts = list(range(close - 2 * 86400, close + 60, 60))
    rng = np.random.default_rng(0)
    spot = (80_000 * np.exp(np.cumsum(rng.normal(0, 3e-4, len(spot_ts))))).tolist()
    pq.write_table(pa.table({"ts": spot_ts, "close": spot}), tmp_path / "spot_1m_BTC-USD.parquet")
    for series in ("KXBTC15M", "KXBTCD"):
        hourly = series == "KXBTCD"
        # the 15-minute files hold one unsettled market and an unrelated candle: no firings
        markets = pa.table(
            {
                "event_ticker": ["EV" if hourly else "OTHER"],
                "ticker": ["EV-T1" if hourly else "OTHER-T1"],
                "open_ts": [close - 3600],
                "close_ts": [close],
                "floor": [spot[-61] * 1.01],
                "result": ["no" if hourly else ""],
            }
        )
        pq.write_table(markets, tmp_path / f"kalshi_markets_{series}.parquet")
        ends = list(range(close - 3600, close, 60)) if hourly else [close - 600]
        pq.write_table(
            pa.table(
                {
                    "ticker": ["EV-T1" if hourly else "UNUSED"] * len(ends),
                    "end_ts": ends,
                    "bid": [0.03] * len(ends),
                    "ask": [0.05] * len(ends),
                    "last": [0.05] * len(ends),
                    "volume": [5.0] * len(ends),
                }
            ),
            tmp_path / f"kalshi_candles_{series}.parquet",
        )
    # the live rule now stops 15 minutes before close; the study keeps the old 5-minute scope
    assert sorted(r.minutes_left for r in firings(tmp_path)) == list(range(15, 61, 5))
    rows = firings(tmp_path, STUDY_CONFIG)
    assert sorted(r.minutes_left for r in rows) == list(range(5, 61, 5))
    assert all(r.filled and not r.won_long and r.long_ask == 0.05 for r in rows)
    assert all(r.features["hourly"] == 1.0 and r.features["z_to_strike"] > 0 for r in rows)
