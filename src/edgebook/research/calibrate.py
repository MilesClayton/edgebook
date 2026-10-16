"""Walk-forward calibration of the pricing model against settled Kalshi markets.

For each settled market and horizon h, the model sees only data up to the
decision time t = close - h: the HAR fit is refreshed once per UTC day using
hours before that day, spot is the 1-minute close at t, and the forecast
variance covers [t, close). Each model is scored against the outcome next to
the market's own mid price, and a naive taker strategy (cross the spread
when model edge beats the fee plus a threshold) gives a first, deliberately
conservative read on whether an edge survives costs.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
import numpy.typing as npt

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.model.calibration import Bucket, MeanCI, brier, log_loss, mean_ci, reliability
from edgebook.model.pricer import contract_prob, prob_settle_above
from edgebook.model.vol import HOUR_S, HarModel, HourlyRV, ewma_var_per_s, hourly_realized

F64 = npt.NDArray[np.float64]
DAY_S = 24 * HOUR_S
WARMUP_DAYS = 8  # HAR needs a week of lags plus fitting rows
MIN_MID, MAX_MID, MAX_SPREAD = 0.03, 0.97, 0.10


@dataclass(frozen=True)
class ModelSpec:
    name: str
    vol: str  # "har" | "ewma"
    nu: float | None


MODELS = (
    ModelSpec("har_normal", "har", None),
    ModelSpec("har_t4", "har", 4.0),
    ModelSpec("ewma_normal", "ewma", None),
)


@dataclass(frozen=True)
class TakerResult:
    threshold: float
    pnl: MeanCI  # per-contract dollars after fees
    total: float


@dataclass(frozen=True)
class ModelScore:
    brier: float
    log_loss: float
    buckets: list[Bucket]
    taker: list[TakerResult]


@dataclass(frozen=True)
class HorizonReport:
    horizon_min: int
    n: int
    market_brier: float
    market_log_loss: float
    models: dict[str, ModelScore]


@dataclass
class CalibrationReport:
    n_rows_in: int
    n_scored: int
    skipped: dict[str, int]
    basis_bps: dict[str, float]
    horizons: list[HorizonReport] = field(default_factory=list)


class _Spot:
    def __init__(self, ts: npt.NDArray[np.int64], close: F64) -> None:
        order = np.argsort(ts)
        self.ts, self.close = ts[order], close[order]
        self.idx = {int(t): i for i, t in enumerate(self.ts)}

    def at(self, t: int) -> float | None:
        i = self.idx.get(t)
        return None if i is None else float(self.close[i])

    def returns_before(self, t: int, minutes: int) -> F64:
        i = self.idx.get(t)
        if i is None:
            return np.array([], dtype=np.float64)
        lo = max(0, i - minutes)
        seg_ts, seg = self.ts[lo : i + 1], self.close[lo : i + 1]
        r: F64 = np.diff(np.log(seg))
        out: F64 = r[np.diff(seg_ts) == 60]
        return out


class _HarCache:
    """One HAR fit per UTC day, using only hours before that day."""

    def __init__(self, hourly: HourlyRV) -> None:
        self.hourly = hourly
        self.fits: dict[int, HarModel | None] = {}

    def model_for(self, t: int) -> HarModel | None:
        day = t // DAY_S * DAY_S
        if day not in self.fits:
            i = self.hourly.index_of(day)
            if i < WARMUP_DAYS * 24:
                self.fits[day] = None
            else:
                past = HourlyRV(self.hourly.hours[:i], self.hourly.rv[:i], self.hourly.bv[:i])
                try:
                    self.fits[day] = HarModel.fit(past)
                except ValueError:
                    self.fits[day] = None
        return self.fits[day]


def _taker_pnl(p: float, bid: float, ask: float, y: int, threshold: float) -> float | None:
    """Per-contract P&L of crossing the spread when edge after fees beats threshold."""
    fee_yes = float(trade_fee(1, Decimal(str(ask)), LiquidityRole.TAKER))
    if p - ask - fee_yes > threshold:
        return y - ask - fee_yes
    no_price = 1 - bid
    fee_no = float(trade_fee(1, Decimal(str(round(no_price, 4))), LiquidityRole.TAKER))
    if (1 - p) - no_price - fee_no > threshold:
        return (1 - y) - no_price - fee_no
    return None


def run(
    spot_ts: npt.NDArray[np.int64],
    spot_close: F64,
    rows: Sequence[dict[str, Any]],
    thresholds: Sequence[float] = (0.0, 0.02, 0.05),
    models: Sequence[ModelSpec] = MODELS,
    progress: Callable[[int, int], None] | None = None,
) -> CalibrationReport:
    spot = _Spot(spot_ts, spot_close)
    har = _HarCache(hourly_realized(spot_ts, spot_close))
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    # Proxy tracking error: Coinbase close at settlement vs Kalshi's settlement value.
    basis = []
    for close_ts, value in {(r["close_ts"], r["expiration_value"]) for r in rows}:
        s = spot.at(int(close_ts))
        if s is not None and value:
            basis.append(abs(s / value - 1) * 1e4)
    basis_arr = np.array(basis) if basis else np.array([np.nan])

    scored: dict[int, dict[str, list[float]]] = {}
    var_cache: dict[tuple[int, int], tuple[float, float]] = {}
    for k, r in enumerate(rows):
        if progress and k % 5000 == 0:
            progress(k, len(rows))
        h = int(r["horizon_min"])
        t = int(r["close_ts"]) - 60 * h
        bid, ask = r["bid"], r["ask"]
        if bid is None or ask is None or math.isnan(bid) or math.isnan(ask):
            skip("no_quote")
            continue
        mid = (bid + ask) / 2
        if not (MIN_MID <= mid <= MAX_MID) or ask - bid > MAX_SPREAD or ask <= bid:
            skip("untradeable_quote")
            continue
        s0 = spot.at(t)
        if s0 is None:
            skip("no_spot")
            continue
        model = har.model_for(t)
        if model is None:
            skip("warmup")
            continue
        seconds = 60 * h
        if (t, seconds) not in var_cache:  # every strike in an event shares these
            var_har = model.variance_between(har.hourly, t, t + seconds) / seconds
            rets = spot.returns_before(t, 240)
            var_cache[(t, seconds)] = (var_har, ewma_var_per_s(rets) if rets.size else var_har)
        var_har, var_ewma = var_cache[(t, seconds)]
        outcome = 1.0 if r["result"] == "yes" else 0.0

        bucket = scored.setdefault(h, {"y": [], "mid": [], "bid": [], "ask": []})
        bucket["y"].append(outcome)
        bucket["mid"].append(mid)
        bucket["bid"].append(bid)
        bucket["ask"].append(ask)
        for spec in models:
            v = var_har if spec.vol == "har" else var_ewma

            p_floor = (
                None
                if r["floor"] is None
                else prob_settle_above(s0, r["floor"], seconds, v, nu=spec.nu)
            )
            p_cap = (
                None
                if r["cap"] is None
                else prob_settle_above(s0, r["cap"], seconds, v, nu=spec.nu)
            )
            p = contract_prob(r["strike_type"], r["floor"], r["cap"], p_floor, p_cap)
            bucket.setdefault(spec.name, []).append(min(max(p, 0.0), 1.0))

    report = CalibrationReport(
        n_rows_in=len(rows),
        n_scored=sum(len(b["y"]) for b in scored.values()),
        skipped=skipped,
        basis_bps={
            "median": float(np.nanmedian(basis_arr)),
            "p90": float(np.nanpercentile(basis_arr, 90)),
            "n": float(len(basis)),
        },
    )
    for h in sorted(scored, reverse=True):
        b = scored[h]
        y = np.array(b["y"])
        mid = np.array(b["mid"])
        model_scores = {}
        for spec in models:
            probs = np.array(b[spec.name], dtype=np.float64)
            taker = []
            for th in thresholds:
                trades = (
                    _taker_pnl(float(pi), bi, ai, int(yi), th)
                    for pi, bi, ai, yi in zip(probs, b["bid"], b["ask"], y, strict=True)
                )
                arr = np.array([x for x in trades if x is not None], dtype=np.float64)
                taker.append(TakerResult(th, mean_ci(arr), float(arr.sum())))
            model_scores[spec.name] = ModelScore(
                brier=brier(probs, y),
                log_loss=log_loss(probs, y),
                buckets=reliability(probs, y),
                taker=taker,
            )
        report.horizons.append(
            HorizonReport(
                horizon_min=h,
                n=int(y.size),
                market_brier=brier(mid, y),
                market_log_loss=log_loss(mid, y),
                models=model_scores,
            )
        )
    return report


def render(report: CalibrationReport) -> str:
    lines = [
        f"rows in: {report.n_rows_in}, scored: {report.n_scored}, skipped: {report.skipped}",
        f"Coinbase-vs-settlement basis: median {report.basis_bps['median']:.1f} bps, "
        f"p90 {report.basis_bps['p90']:.1f} bps (n={int(report.basis_bps['n'])})",
    ]
    for hr in report.horizons:
        lines.append("")
        lines.append(
            f"== {hr.horizon_min} min before close: n={hr.n}  market Brier {hr.market_brier:.4f}"
            f"  logloss {hr.market_log_loss:.4f}"
        )
        for name, ms in hr.models.items():
            ok = sum(b.calibrated for b in ms.buckets if b.n)
            used = sum(1 for b in ms.buckets if b.n)
            lines.append(
                f"  {name:<12} Brier {ms.brier:.4f}  logloss {ms.log_loss:.4f}"
                f"  calibrated buckets {ok}/{used}"
            )
            for tr in ms.taker:
                lines.append(
                    f"    taker edge>{tr.threshold:.2f}: {tr.pnl.n:>5} trades, "
                    f"mean {tr.pnl.mean:+.4f} [{tr.pnl.ci_lo:+.4f}, {tr.pnl.ci_hi:+.4f}]"
                    f" $/contract, total {tr.total:+.2f}"
                )
    return "\n".join(lines)
