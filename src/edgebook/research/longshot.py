"""Favourite-longshot bias on Kalshi BTC markets.

In many betting markets cheap long-shot contracts win less often than their
price implies (and favourites slightly more), so buying the favourite side is
profitable. Here every settled market in the 1-minute quote backfill is
looked at a fixed time before its close, bucketed by price, and scored two
ways: buying YES at the ask, and buying NO at 1 - the YES bid, each held to the
result after Kalshi's taker fee.

Strikes of one hourly event move together, so confidence intervals come from
a bootstrap over events (not over markets), and the sample is split in time
to check that a bias found in the first half is still there in the second.
"""

from __future__ import annotations

import bisect
import itertools
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from edgebook.research.edge_scan import taker_fee

BUCKETS = (0.02, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 0.98)
HORIZONS_MIN = (60, 30, 15, 10, 5, 2)


@dataclass(frozen=True)
class Quote:
    event: str
    ticker: str
    close_ts: int
    horizon_min: int
    yes_bid: float
    yes_ask: float
    won_yes: bool


def quotes(history_dir: Path, series: str, horizons: Sequence[int] = HORIZONS_MIN) -> list[Quote]:
    """Best YES bid/ask at each horizon before close, for every settled market."""
    markets = pq.read_table(history_dir / f"kalshi_markets_{series}.parquet").to_pylist()
    candles = pq.read_table(
        history_dir / f"kalshi_candles_{series}.parquet", columns=["ticker", "end_ts", "bid", "ask"]
    ).to_pylist()
    by_ticker: dict[str, list[tuple[int, float | None, float | None]]] = defaultdict(list)
    for c in candles:
        by_ticker[c["ticker"]].append((c["end_ts"], c["bid"], c["ask"]))
    out = []
    for m in markets:
        if m["result"] not in ("yes", "no"):
            continue
        rows = sorted(by_ticker.get(m["ticker"], []))
        ends = [r[0] for r in rows]
        for h in horizons:
            t = m["close_ts"] - h * 60
            i = bisect.bisect_right(ends, t) - 1
            if i < 0 or t - ends[i] > 120:  # need a quote from the last two minutes
                continue
            _, bid, ask = rows[i]
            if bid is None or ask is None or not 0 < bid < ask < 1:
                continue
            out.append(
                Quote(
                    m["event_ticker"], m["ticker"], m["close_ts"], h, bid, ask, m["result"] == "yes"
                )
            )
    return out


def _bootstrap_by_event(
    pnl: np.ndarray, events: np.ndarray, n_boot: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """95% CI of the per-contract mean, resampling whole events."""
    uniq, inv = np.unique(events, return_inverse=True)
    sums = np.bincount(inv, weights=pnl)
    counts = np.bincount(inv)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
    means = sums[idx].sum(axis=1) / np.maximum(counts[idx].sum(axis=1), 1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def bucket_report(qs: Sequence[Quote], horizon: int) -> list[dict[str, Any]]:
    """Per YES-mid bucket: how often YES won vs its price, and buy-YES / buy-NO results."""
    sel = [q for q in qs if q.horizon_min == horizon]
    rows = []
    for lo, hi in itertools.pairwise(BUCKETS):
        b = [q for q in sel if lo <= (q.yes_bid + q.yes_ask) / 2 < hi]
        if not b:
            continue
        won = np.array([q.won_yes for q in b], dtype=float)
        mid = np.array([(q.yes_bid + q.yes_ask) / 2 for q in b])
        ev = np.array([q.event for q in b])
        yes_pnl = np.array(
            [w - q.yes_ask - taker_fee(q.yes_ask) for q, w in zip(b, won, strict=True)]
        )
        no_px = np.array([1 - q.yes_bid for q in b])
        no_pnl = np.array([(1 - w) - p - taker_fee(p) for p, w in zip(no_px, won, strict=True)])
        rows.append(
            {
                "bucket": f"{lo:.2f}-{hi:.2f}",
                "n": len(b),
                "events": len(set(ev)),
                "avg_mid": float(mid.mean()),
                "yes_won": float(won.mean()),
                "buy_yes": float(yes_pnl.mean()),
                "buy_yes_ci": _bootstrap_by_event(yes_pnl, ev),
                "buy_no": float(no_pnl.mean()),
                "buy_no_ci": _bootstrap_by_event(no_pnl, ev),
            }
        )
    return rows


def split_in_time(qs: Sequence[Quote]) -> tuple[list[Quote], list[Quote]]:
    cut = float(np.median([q.close_ts for q in qs]))
    return [q for q in qs if q.close_ts <= cut], [q for q in qs if q.close_ts > cut]
