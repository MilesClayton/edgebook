"""Who makes money: makers or takers? Measured on settled Kalshi markets.

Every public trade names the aggressor (``taker_side``). For a settled market
the taker's P&L per contract is ``payout - price - taker_fee`` and the resting
(maker) side's is the mirror image minus the maker fee. If makers earn a
reliable premium after fees, passive quoting (market making, paired bids) is
a candidate edge; if not, no fill model will rescue it.

This is an upper bound for *our* maker P&L: real makers also pay queue
priority, cancel latency, and adverse selection we would share.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from edgebook.core.types import LiquidityRole
from edgebook.history.kalshi import load_markets
from edgebook.model.calibration import MeanCI, mean_ci
from edgebook.recorder.rows import make_schema
from edgebook.venues.kalshi.rest import KalshiRest

TRADES = make_schema(
    ("ticker", pa.string()),
    ("ts", pa.int64()),
    ("yes_price", pa.float64()),
    ("count", pa.float64()),
    ("taker_side", pa.string()),
)


def trades_path(directory: Path, series: str) -> Path:
    return directory / f"kalshi_trades_{series}.parquet"


async def backfill_trades(rest: KalshiRest, directory: Path, series: str) -> int:
    """Fetch trades for every stored settled market not yet fetched."""
    path = trades_path(directory, series)
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist() if path.exists() else []
    done = {r["ticker"] for r in rows}
    added = 0
    for m in load_markets(directory, series):
        if m["ticker"] in done:
            continue
        for t in await rest.market_trades(m["ticker"]):
            rows.append(
                {
                    "ticker": t.ticker,
                    "ts": int(t.created_time.timestamp()),
                    "yes_price": float(t.yes_price_dollars),
                    "count": float(t.count_fp),
                    "taker_side": t.taker_side.value,
                }
            )
            added += 1
        if not added % 50_000:
            pq.write_table(pa.Table.from_pylist(rows, schema=TRADES), path)
    pq.write_table(pa.Table.from_pylist(rows, schema=TRADES), path)
    return added


def _fee(p: float, role: LiquidityRole) -> float:
    """Per-contract fee (unrounded: fees round per order, and orders bundle many contracts)."""
    q = min(max(p, 0.0), 1.0)
    coef = Decimal("0.07") if role is LiquidityRole.TAKER else Decimal("0.0175")
    return float(coef * Decimal(f"{q:.4f}") * (1 - Decimal(f"{q:.4f}")))


@dataclass(frozen=True)
class FlowBucket:
    label: str
    contracts: float
    trades: int
    taker: MeanCI  # contract-weighted mean P&L per contract (trade-level CI)
    maker: MeanCI


def analyze(
    trades: Sequence[dict[str, Any]],
    markets: Sequence[dict[str, Any]],
    by: str = "price",
) -> list[FlowBucket]:
    result = {m["ticker"]: m for m in markets if m["result"] in ("yes", "no")}
    close = {m["ticker"]: m["close_ts"] for m in markets}
    groups: dict[str, list[tuple[float, float, float]]] = {}
    for t in trades:
        m = result.get(t["ticker"])
        if m is None:
            continue
        taker_yes = t["taker_side"] == "yes"
        p_taker = t["yes_price"] if taker_yes else 1 - t["yes_price"]
        won = (m["result"] == "yes") == taker_yes
        taker_pnl = (1.0 if won else 0.0) - p_taker - _fee(p_taker, LiquidityRole.TAKER)
        p_maker = 1 - p_taker
        maker_pnl = (0.0 if won else 1.0) - p_maker - _fee(p_maker, LiquidityRole.MAKER)
        if by == "price":
            edges = (0.0, 0.05, 0.15, 0.35, 0.65, 0.85, 0.95, 1.01)
            k = next(i for i, e in enumerate(edges[1:]) if p_taker < e)
            label = f"taker pays {edges[k]:.2f}-{min(edges[k + 1], 1):.2f}"
        else:
            left = (close[t["ticker"]] - t["ts"]) / 60
            label = next(
                f"{lo}-{hi} min left"
                for lo, hi in ((0, 2), (2, 5), (5, 10), (10, 30), (30, 10**6))
                if lo <= left < hi
            )
        groups.setdefault(label, []).append((t["count"], taker_pnl, maker_pnl))
    out = []
    for label in sorted(groups):
        g = np.array(groups[label], dtype=np.float64)
        w = g[:, 0]
        # contract-weighted means; CI from per-trade values weighted by repetition is
        # approximated with per-trade means (conservative for large block trades)
        out.append(
            FlowBucket(
                label,
                float(w.sum()),
                len(g),
                _wmean_ci(g[:, 1], w),
                _wmean_ci(g[:, 2], w),
            )
        )
    return out


def _wmean_ci(
    x: np.ndarray[Any, np.dtype[np.float64]], w: np.ndarray[Any, np.dtype[np.float64]]
) -> MeanCI:
    base = mean_ci(x)
    if base.n < 2 or w.sum() <= 0:
        return base
    m = float(np.average(x, weights=w))
    half = (base.ci_hi - base.ci_lo) / 2
    return MeanCI(base.n, m, m - half, m + half)


def render(series: str, buckets: Sequence[FlowBucket]) -> str:
    lines = [
        f"### {series}: P&L per contract after fees, by role ({datetime.now(UTC):%Y-%m-%d})",
        "  bucket                       contracts  trades |  taker $/ct [CI]"
        "          |  maker $/ct [CI]",
    ]
    for b in buckets:
        lines.append(
            f"  {b.label:<27} {b.contracts:>10,.0f} {b.trades:>7} | {b.taker.mean:+.4f}"
            f" [{b.taker.ci_lo:+.4f},{b.taker.ci_hi:+.4f}] | {b.maker.mean:+.4f}"
            f" [{b.maker.ci_lo:+.4f},{b.maker.ci_hi:+.4f}]"
        )
    return "\n".join(lines)
