"""Walk-forward strategy search on point-in-time features.

Each strategy maps features to a probability. For every test day it is fit on
all earlier days only (expanding window, at least ``MIN_TRAIN_DAYS``), then
predicts that day, so every reported number is out-of-sample. A strategy
trades as a taker when its probability beats the ask (or 1 - bid for NO) by
the fee plus a threshold.

The last ``HOLDOUT_DAYS`` are reported separately: a strategy that only works
on early days is not a strategy. With several strategies and thresholds, a
single positive CI is expected by chance; look for consistency across both
periods before believing anything.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import numpy.typing as npt
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.model.calibration import MeanCI, brier, mean_ci
from edgebook.research.calibrate import taker_pnl
from edgebook.research.features import FEATURES, FeatureSet

F64 = npt.NDArray[np.float64]
MIN_TRAIN_DAYS = 7
HOLDOUT_DAYS = 7
THRESHOLDS = (0.0, 0.02, 0.05)

Fitter = Callable[[F64, F64], Callable[[F64], F64]]


def _col(*names: str) -> list[int]:
    return [FEATURES.index(n) for n in names]


def _logreg(cols: list[int], c: float = 1.0) -> Fitter:
    def fit(x: F64, y: F64) -> Callable[[F64], F64]:
        scaler = StandardScaler().fit(x[:, cols])
        clf = LogisticRegression(C=c, max_iter=1000).fit(scaler.transform(x[:, cols]), y)

        def predict(xt: F64) -> F64:
            out: F64 = clf.predict_proba(scaler.transform(xt[:, cols]))[:, 1]
            return out

        return predict

    return fit


def _gbm(x: F64, y: F64) -> Callable[[F64], F64]:
    clf = HistGradientBoostingClassifier(
        max_depth=3, learning_rate=0.05, max_iter=200, l2_regularization=1.0, random_state=0
    ).fit(x, y)

    def predict(xt: F64) -> F64:
        out: F64 = clf.predict_proba(xt)[:, 1]
        return out

    return predict


def _fixed(col: str) -> Fitter:
    i = FEATURES.index(col)

    def fit(x: F64, y: F64) -> Callable[[F64], F64]:
        def predict(xt: F64) -> F64:
            out: F64 = 1 / (1 + np.exp(-xt[:, i]))
            return out

        return predict

    return fit


STRATEGIES: dict[str, Fitter] = {
    "market": _fixed("logit_mid"),
    "model_t4": _fixed("logit_model"),
    "blend": _logreg(_col("logit_mid", "logit_model")),
    "logreg_all": _logreg(list(range(len(FEATURES))), c=0.3),
    "gbm": _gbm,
}


@dataclass(frozen=True)
class PeriodResult:
    n: int
    brier: float
    market_brier: float
    taker: dict[float, MeanCI]


@dataclass(frozen=True)
class StrategyResult:
    name: str
    all_oos: PeriodResult
    holdout: PeriodResult
    equity: list[tuple[int, float]]  # (decision ts, cumulative $) for taker edge > 2c, all OOS


def _equity(fs: FeatureSet, p: F64, th: float = 0.02) -> list[tuple[int, float]]:
    trades = sorted(
        (int(t), pnl)
        for t, pi, bi, ai, yi in zip(fs.t, p, fs.bid, fs.ask, fs.y, strict=True)
        if not np.isnan(pi)
        for pnl in [taker_pnl(float(pi), float(bi), float(ai), int(yi), th)]
        if pnl is not None
    )
    total, out = 0.0, []
    for t, pnl in trades:
        total += pnl
        out.append((t, round(total, 4)))
    return out


def _period(fs: FeatureSet, p: F64, sel: npt.NDArray[np.bool_]) -> PeriodResult:
    taker = {}
    for th in THRESHOLDS:
        pnls = [
            x
            for x in (
                taker_pnl(float(pi), float(bi), float(ai), int(yi), th)
                for pi, bi, ai, yi in zip(p[sel], fs.bid[sel], fs.ask[sel], fs.y[sel], strict=True)
            )
            if x is not None
        ]
        taker[th] = mean_ci(np.array(pnls, dtype=np.float64))
    n = int(sel.sum())
    return PeriodResult(
        n=n,
        brier=brier(p[sel], fs.y[sel]) if n else float("nan"),
        market_brier=brier(fs.mid[sel], fs.y[sel]) if n else float("nan"),
        taker=taker,
    )


def walk_forward(fs: FeatureSet, fitter: Fitter) -> F64:
    """Out-of-sample probability for every row (NaN on the training warmup)."""
    p = np.full(fs.y.size, np.nan)
    days = np.unique(fs.day)
    for d in days[MIN_TRAIN_DAYS:]:
        train, test = fs.day < d, fs.day == d
        if len(np.unique(fs.y[train])) < 2:
            continue
        predict = fitter(fs.x[train], fs.y[train])
        p[test] = np.clip(predict(fs.x[test]), 0.0, 1.0)
    return p


def run(fs: FeatureSet, names: Sequence[str] | None = None) -> list[StrategyResult]:
    days = np.unique(fs.day)
    holdout_start = days[-HOLDOUT_DAYS] if days.size > HOLDOUT_DAYS else days[0]
    results = []
    for name in names or STRATEGIES:
        p = walk_forward(fs, STRATEGIES[name])
        scored = ~np.isnan(p)
        results.append(
            StrategyResult(
                name=name,
                all_oos=_period(fs, p, scored),
                holdout=_period(fs, p, scored & (fs.day >= holdout_start)),
                equity=_equity(fs, p),
            )
        )
    return results


def buy_yes_pnl(ask: float, y: int) -> float:
    return y - ask - float(trade_fee(1, Decimal(f"{ask:.4f}"), LiquidityRole.TAKER))


def buy_no_pnl(bid: float, y: int) -> float:
    no_price = 1 - bid
    return (1 - y) - no_price - float(trade_fee(1, Decimal(f"{no_price:.4f}"), LiquidityRole.TAKER))


@dataclass(frozen=True)
class BiasBucket:
    lo: float
    hi: float
    n: int
    yes: MeanCI  # buy YES at the ask, $/contract after fee
    no: MeanCI  # buy NO at 1 - bid, $/contract after fee


def price_bias(
    fs: FeatureSet, edges: Sequence[float] = (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0)
) -> list[BiasBucket]:
    """Taker P&L of blindly buying YES at the ask or NO at 1 - bid, by mid bucket."""
    out = []
    for lo, hi in itertools.pairwise(edges):
        sel = (fs.mid >= lo) & (fs.mid < hi)
        rows = list(zip(fs.bid[sel], fs.ask[sel], fs.y[sel], strict=True))
        yes = [buy_yes_pnl(float(a), int(y)) for _, a, y in rows]
        no = [buy_no_pnl(float(b), int(y)) for b, _, y in rows]
        out.append(
            BiasBucket(
                lo=lo,
                hi=hi,
                n=int(sel.sum()),
                yes=mean_ci(np.array(yes)),
                no=mean_ci(np.array(no)),
            )
        )
    return out


def render_bias(buckets: Sequence[BiasBucket]) -> str:
    lines = ["  mid bucket     n    buy YES @ask $/ct          buy NO @(1-bid) $/ct"]
    for b in buckets:
        lines.append(
            f"  [{b.lo:.2f},{b.hi:.2f}) {b.n:>6}"
            f"  {b.yes.mean:+.4f} [{b.yes.ci_lo:+.4f},{b.yes.ci_hi:+.4f}]"
            f"  {b.no.mean:+.4f} [{b.no.ci_lo:+.4f},{b.no.ci_hi:+.4f}]"
        )
    return "\n".join(lines)


def render(series: str, fs: FeatureSet, results: Sequence[StrategyResult]) -> str:
    lines = [f"### {series}: {fs.y.size} rows, skipped {fs.skipped}"]
    for r in results:
        for label, pr in (("all OOS", r.all_oos), ("holdout", r.holdout)):
            lines.append(
                f"  {r.name:<11} {label:<8} n={pr.n:<6} Brier {pr.brier:.4f}"
                f" (mkt {pr.market_brier:.4f})"
            )
            for th, ci in pr.taker.items():
                lines.append(
                    f"      taker edge>{th:.2f}: {ci.n:>5} trades {ci.mean:+.4f} "
                    f"[{ci.ci_lo:+.4f}, {ci.ci_hi:+.4f}] $/ct"
                )
    return "\n".join(lines)
