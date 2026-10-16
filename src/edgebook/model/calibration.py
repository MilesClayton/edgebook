"""Forecast scoring: Brier, log loss, reliability buckets, mean with CI."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

F64 = npt.NDArray[np.float64]
Z95 = 1.959963984540054


def _check(p: F64, y: F64) -> None:
    if p.shape != y.shape or p.ndim != 1 or p.size == 0:
        raise ValueError("p and y must be non-empty 1-D arrays of equal length")
    if np.any((p < 0) | (p > 1)) or not np.all((y == 0) | (y == 1)):
        raise ValueError("p must be in [0, 1] and y must be 0/1")


def brier(p: F64, y: F64) -> float:
    _check(p, y)
    return float(np.mean((p - y) ** 2))


def log_loss(p: F64, y: F64, eps: float = 1e-6) -> float:
    _check(p, y)
    q = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    phat = successes / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass(frozen=True)
class Bucket:
    lo: float
    hi: float
    n: int
    mean_p: float
    freq: float
    ci_lo: float
    ci_hi: float

    @property
    def calibrated(self) -> bool:
        return self.n == 0 or self.ci_lo <= self.mean_p <= self.ci_hi


def reliability(p: F64, y: F64, n_buckets: int = 10) -> list[Bucket]:
    _check(p, y)
    edges = np.linspace(0, 1, n_buckets + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, n_buckets - 1)
    out = []
    for k in range(n_buckets):
        sel = idx == k
        n = int(sel.sum())
        hits = int(y[sel].sum())
        lo, hi = wilson(hits, n)
        out.append(
            Bucket(
                lo=float(edges[k]),
                hi=float(edges[k + 1]),
                n=n,
                mean_p=float(p[sel].mean()) if n else float("nan"),
                freq=hits / n if n else float("nan"),
                ci_lo=lo,
                ci_hi=hi,
            )
        )
    return out


@dataclass(frozen=True)
class MeanCI:
    n: int
    mean: float
    ci_lo: float
    ci_hi: float


def mean_ci(x: F64) -> MeanCI:
    """Mean with a normal-approximation 95% CI (NaN CI below 2 samples)."""
    n = int(x.size)
    if n == 0:
        return MeanCI(0, float("nan"), float("nan"), float("nan"))
    m = float(x.mean())
    if n < 2:
        return MeanCI(n, m, float("nan"), float("nan"))
    half = Z95 * float(x.std(ddof=1)) / math.sqrt(n)
    return MeanCI(n, m, m - half, m + half)
