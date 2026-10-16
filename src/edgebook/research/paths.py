"""Round-trip (enter, then exit before settlement) strategies on 1-minute paths.

Settlement-hold tests can't see edges that come from the contract price
*catching up*: e.g. spot jumps, the Kalshi quote lags, you buy, and you sell
a minute or two later once it reprices. These rules trade the quote path:

- enter at the ask (taker) when a signal fires at a minute close
- exit at the bid (taker) on take-profit / stop-loss / max hold, or at the last
  minute before the 60s averaging window, whichever comes first; if a position
  is still open at close it settles at $1/$0
- both legs pay Kalshi's taker fee; one position per market at a time

Quotes are 1-minute closes, so only lags of about a minute or more are
visible; second-level lags need the tick backtester on recorder data.
Parameters are chosen on the train period and judged on a later holdout.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.history.kalshi import tradeable
from edgebook.model.calibration import MeanCI, mean_ci
from edgebook.research.calibrate import Spot


def fee(price: float) -> float:
    p = min(max(round(price, 4), 0.0), 1.0)
    return float(trade_fee(1, Decimal(f"{p:.4f}"), LiquidityRole.TAKER))


@dataclass(frozen=True)
class LagParams:
    """Enter in the direction of a spot move the contract hasn't priced yet."""

    window_min: int  # spot move measured over this many minutes
    min_move_pct: float  # e.g. 0.08 means 0.08%
    max_mid_follow: float  # skip if the YES mid already moved this much in our direction
    take_profit: float  # fraction of entry price, e.g. 0.15
    stop_loss: float  # fraction of entry price, e.g. 0.25
    max_hold_min: int
    max_entry: float = 0.85  # CloddsBot skips contracts above 85c


@dataclass(frozen=True)
class Trade:
    ticker: str
    entry_ts: int
    exit_ts: int
    side: str  # "yes" | "no"
    entry: float  # price paid for the side bought (incl. nothing else)
    exit: float  # price received (or settlement 0/1)
    pnl: float  # $/contract after both fees
    reason: str


def _side_quotes(c: dict[str, Any], side: str) -> tuple[float, float]:
    """(buy price, sell price) for the chosen side from a YES bid/ask candle."""
    if side == "yes":
        return c["ask"], c["bid"]
    return 1 - c["bid"], 1 - c["ask"]


def simulate_lag(
    markets: Sequence[dict[str, Any]],
    candles: dict[str, list[dict[str, Any]]],
    spot: Spot,
    params: LagParams,
) -> list[Trade]:
    trades: list[Trade] = []
    for m in markets:
        cs = [c for c in candles.get(m["ticker"], []) if c["end_ts"] <= m["close_ts"] - 60]
        sign = -1.0 if m["strike_type"] in ("less", "less_or_equal") else 1.0
        y = 1 if m["result"] == "yes" else 0
        i = 0
        while i < len(cs):
            c = cs[i]
            t = c["end_ts"]
            s_now, s_then = spot.at(t), spot.at(t - 60 * params.window_min)
            prev = next(
                (p for p in reversed(cs[:i]) if p["end_ts"] <= t - 60 * params.window_min), None
            )
            if s_now is None or s_then is None or prev is None or not tradeable(c["bid"], c["ask"]):
                i += 1
                continue
            move_pct = (s_now / s_then - 1) * 100 * sign
            mid_move = ((c["bid"] + c["ask"]) - (prev["bid"] + prev["ask"])) / 2
            if abs(move_pct) < params.min_move_pct:
                i += 1
                continue
            side = "yes" if move_pct > 0 else "no"
            followed = mid_move if side == "yes" else -mid_move
            buy, _ = _side_quotes(c, side)
            if followed >= params.max_mid_follow or buy > params.max_entry or buy <= 0:
                i += 1
                continue
            entry_cost = buy + fee(buy)
            exit_ts, exit_px, reason = m["close_ts"], float(y if side == "yes" else 1 - y), "settle"
            j = i + 1
            while j < len(cs):
                _, sell = _side_quotes(cs[j], side)
                held = (cs[j]["end_ts"] - t) // 60
                last = j == len(cs) - 1
                if sell >= buy * (1 + params.take_profit):
                    reason = "tp"
                elif sell <= buy * (1 - params.stop_loss):
                    reason = "sl"
                elif held >= params.max_hold_min:
                    reason = "time"
                elif last:
                    reason = "pre_window"
                else:
                    j += 1
                    continue
                exit_ts, exit_px = cs[j]["end_ts"], sell
                break
            proceeds = exit_px - (fee(exit_px) if reason != "settle" else 0.0)
            trades.append(
                Trade(m["ticker"], t, exit_ts, side, buy, exit_px, proceeds - entry_cost, reason)
            )
            # one position at a time: resume after the exit
            i = next((k for k in range(j, len(cs)) if cs[k]["end_ts"] > exit_ts), len(cs))
    return trades


def grid() -> list[LagParams]:
    return [
        LagParams(w, mv, mf, tp, sl, hold)
        for w, mv, mf, tp, sl, hold in itertools.product(
            (1, 2, 3), (0.08, 0.15, 0.25), (0.02, 0.05), (0.15, 0.30), (0.25,), (2, 5)
        )
    ]


@dataclass(frozen=True)
class GridResult:
    params: LagParams
    train: MeanCI
    holdout: MeanCI
    holdout_trades: int


def search(
    markets: Sequence[dict[str, Any]],
    candles: dict[str, list[dict[str, Any]]],
    spot: Spot,
    holdout_start: int,
    params: Sequence[LagParams] | None = None,
) -> list[GridResult]:
    """Evaluate every parameter set; rank by *train* mean so the holdout stays honest."""
    train_m = [m for m in markets if m["close_ts"] < holdout_start]
    hold_m = [m for m in markets if m["close_ts"] >= holdout_start]
    out = []
    for p in params or grid():
        tr = simulate_lag(train_m, candles, spot, p)
        ho = simulate_lag(hold_m, candles, spot, p)
        out.append(
            GridResult(
                p,
                mean_ci(np.array([x.pnl for x in tr])),
                mean_ci(np.array([x.pnl for x in ho])),
                len(ho),
            )
        )
    out.sort(
        key=lambda g: g.train.mean if g.train.n >= 30 and math.isfinite(g.train.mean) else -9,
        reverse=True,
    )
    return out


def render(series: str, results: Sequence[GridResult], top: int = 8) -> str:
    lines = [
        f"### {series} lag round-trips ({len(results)} parameter sets; ranked on TRAIN only)",
        "  win mv%  follow tp   sl   hold | train n   mean $/ct [95% CI]        |"
        " holdout n  mean $/ct [95% CI]",
    ]
    for g in results[:top]:
        p = g.params
        lines.append(
            f"  {p.window_min:>3} {p.min_move_pct:>4.2f} {p.max_mid_follow:>6.2f}"
            f" {p.take_profit:.2f}"
            f" {p.stop_loss:.2f} {p.max_hold_min:>4} | {g.train.n:>7} {g.train.mean:+.4f}"
            f" [{g.train.ci_lo:+.4f},{g.train.ci_hi:+.4f}] | {g.holdout.n:>9} {g.holdout.mean:+.4f}"
            f" [{g.holdout.ci_lo:+.4f},{g.holdout.ci_hi:+.4f}]"
        )
    return "\n".join(lines)
