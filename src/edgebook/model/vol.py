"""Volatility forecasting from 1-minute prices.

Two forecasters, compared by calibration:

- ``ewma_var_per_s``: exponentially weighted per-minute variance. A fast,
  assumption-light baseline.
- ``HarModel``: HAR-RV on hourly realized variance (lags: last hour, last day,
  last week) with a jump regressor (realized minus bipower variation) and an
  hour-of-day seasonal profile. Fit in logs on deseasonalized RV, then
  reseasonalized with a lognormal bias correction.

All variances are of log price. Timestamps are Unix seconds; a 1-minute close
at ``ts`` is the price at ``ts``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

F64 = npt.NDArray[np.float64]
I64 = npt.NDArray[np.int64]

HOUR_S = 3600
MIN_S = 60
MIN_MINUTES_PER_HOUR = 50  # hours with fewer returns are treated as missing
LAGS = (1, 24, 168)


@dataclass(frozen=True)
class HourlyRV:
    """Realized (rv) and bipower (bv) variance per UTC hour; NaN when sparse."""

    hours: I64  # hour start, Unix seconds, contiguous
    rv: F64
    bv: F64

    def index_of(self, hour_start: int) -> int:
        return int((hour_start - int(self.hours[0])) // HOUR_S)


def minute_returns(ts: I64, close: F64) -> tuple[I64, F64]:
    """Log returns between consecutive minutes; returns spanning a gap are dropped."""
    if ts.shape != close.shape or ts.ndim != 1:
        raise ValueError("ts and close must be 1-D arrays of equal length")
    order = np.argsort(ts, kind="stable")
    ts, close = ts[order], close[order]
    r = np.diff(np.log(close))
    end = ts[1:]
    keep = np.diff(ts) == MIN_S
    return end[keep], r[keep]


def hourly_realized(ts: I64, close: F64) -> HourlyRV:
    end, r = minute_returns(ts, close)
    if end.size == 0:
        raise ValueError("no consecutive minutes to compute returns from")
    hour = (end - 1) // HOUR_S * HOUR_S  # a return ending at hh:00 belongs to the prior hour
    first, last = int(hour.min()), int(hour.max())
    n_hours = (last - first) // HOUR_S + 1
    idx = ((hour - first) // HOUR_S).astype(np.int64)

    count = np.bincount(idx, minlength=n_hours).astype(np.float64)
    rv = np.bincount(idx, weights=r * r, minlength=n_hours)

    # Bipower: (pi/2) * sum |r_i||r_{i-1}| over adjacent returns within the same hour.
    same = (np.diff(end) == MIN_S) & (idx[1:] == idx[:-1])
    prod = np.abs(r[1:]) * np.abs(r[:-1])
    bv = (math.pi / 2) * np.bincount(idx[1:][same], weights=prod[same], minlength=n_hours)
    pairs = np.bincount(idx[1:][same], minlength=n_hours).astype(np.float64)

    with np.errstate(divide="ignore", invalid="ignore"):
        rv_full = rv * (60.0 / count)
        bv_full = bv * (59.0 / pairs)
    sparse = count < MIN_MINUTES_PER_HOUR
    rv_full[sparse] = np.nan
    bv_full[sparse] = np.nan
    hours = first + HOUR_S * np.arange(n_hours, dtype=np.int64)
    return HourlyRV(hours=hours, rv=rv_full, bv=bv_full)


def ewma_var_per_s(r: F64, halflife_min: float = 60.0) -> float:
    """EWMA of squared 1-minute returns (most recent last), per second."""
    if r.size == 0:
        raise ValueError("need at least one return")
    decay = 0.5 ** (1.0 / halflife_min)
    weights = decay ** np.arange(r.size - 1, -1, -1, dtype=np.float64)
    return float(np.sum(weights * r * r) / np.sum(weights)) / MIN_S


def _trailing_mean(x: F64, window: int) -> F64:
    """Mean of x[t-window .. t-1] for each t, ignoring NaN; NaN if under half present."""
    valid = ~np.isnan(x)
    csum = np.concatenate([[0.0], np.cumsum(np.where(valid, x, 0.0))])
    ccnt = np.concatenate([[0], np.cumsum(valid)])
    t = np.arange(x.size)
    lo = np.maximum(t - window, 0)
    s = csum[t] - csum[lo]
    n = (ccnt[t] - ccnt[lo]).astype(np.float64)
    out = np.full(x.size, np.nan)
    ok = n >= max(1, window // 2)
    out[ok] = s[ok] / n[ok]
    return out


@dataclass(frozen=True)
class HarModel:
    coef: F64  # intercept, lag-1h, lag-24h, lag-168h, jump share
    seasonal: F64  # 24 hour-of-day multipliers, mean 1
    resid_var: float
    n_obs: int

    @staticmethod
    def _features(h: HourlyRV, seasonal: F64) -> tuple[F64, F64]:
        hod = (h.hours // HOUR_S) % 24
        d = h.rv / seasonal[hod]
        log_d = np.log(np.maximum(d, 1e-14))
        log_d[np.isnan(d)] = np.nan
        cols = [np.ones(d.size)]
        for lag in LAGS:
            cols.append(np.log(np.maximum(_trailing_mean(d, lag), 1e-14)))
        with np.errstate(divide="ignore", invalid="ignore"):
            jump = np.clip((h.rv - h.bv) / h.rv, 0.0, 1.0)
        cols.append(np.concatenate([[np.nan], jump[:-1]]))
        return np.column_stack(cols), log_d

    @classmethod
    def fit(cls, h: HourlyRV) -> HarModel:
        valid = ~np.isnan(h.rv)
        hod = (h.hours // HOUR_S) % 24
        overall = np.nanmean(h.rv)
        seasonal = np.ones(24)
        for k in range(24):
            sel = valid & (hod == k)
            if sel.sum() >= 5:
                seasonal[k] = np.nanmean(h.rv[sel]) / overall
        seasonal /= seasonal.mean()

        x, y = cls._features(h, seasonal)
        rows = ~np.isnan(y) & ~np.isnan(x).any(axis=1)
        if rows.sum() < 3 * x.shape[1]:
            raise ValueError(f"not enough complete hours to fit HAR ({int(rows.sum())})")
        coef, *_ = np.linalg.lstsq(x[rows], y[rows], rcond=None)
        resid = y[rows] - x[rows] @ coef
        return cls(
            coef=coef, seasonal=seasonal, resid_var=float(resid.var()), n_obs=int(rows.sum())
        )

    def hour_variance(self, h: HourlyRV, hour_start: int) -> float:
        """Forecast log-price variance for the hour starting at ``hour_start``.

        Uses only hours strictly before ``hour_start``.
        """
        i = h.index_of(hour_start)
        if i <= 0 or i > h.hours.size:
            raise ValueError("hour_start must follow at least one recorded hour")
        hist = HourlyRV(hours=h.hours[:i], rv=h.rv[:i], bv=h.bv[:i])
        # Append a placeholder row for the target hour so lags line up.
        padded = HourlyRV(
            hours=np.append(hist.hours, hour_start).astype(np.int64),
            rv=np.append(hist.rv, np.nan),
            bv=np.append(hist.bv, np.nan),
        )
        x, _ = self._features(padded, self.seasonal)
        row = x[-1]
        if np.isnan(row).any():
            row = np.where(np.isnan(row), np.nanmean(x[:-1], axis=0), row)
        log_d = float(row @ self.coef)
        hod = (hour_start // HOUR_S) % 24
        return math.exp(log_d + self.resid_var / 2) * float(self.seasonal[hod])

    def variance_between(self, h: HourlyRV, start_s: int, end_s: int) -> float:
        """Forecast variance over [start_s, end_s), split across hour boundaries."""
        if end_s <= start_s:
            return 0.0
        first_hour = start_s // HOUR_S * HOUR_S
        var = 0.0
        hour = first_hour
        while hour < end_s:
            lo, hi = max(start_s, hour), min(end_s, hour + HOUR_S)
            # Forecast every hour from data before the hour containing start_s.
            per_hour = self.hour_variance(h, first_hour) * (
                self.seasonal[(hour // HOUR_S) % 24] / self.seasonal[(first_hour // HOUR_S) % 24]
            )
            var += per_hour * (hi - lo) / HOUR_S
            hour += HOUR_S
        return var
