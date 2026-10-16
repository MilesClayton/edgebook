import math

import numpy as np
import pytest

from edgebook.model.vol import (
    HOUR_S,
    HarModel,
    ewma_var_per_s,
    hourly_realized,
    minute_returns,
)

T0 = 1_790_000_000 // HOUR_S * HOUR_S


def gbm(
    hours: int, sigma_per_min: np.ndarray | float, seed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    n = hours * 60 + 1
    sig = np.broadcast_to(np.asarray(sigma_per_min, dtype=np.float64), (n - 1,))
    r = rng.normal(0, 1, n - 1) * sig
    close = 80_000 * np.exp(np.concatenate([[0.0], np.cumsum(r)]))
    ts = T0 + 60 * np.arange(n, dtype=np.int64)
    return ts, close


def seasonal_sigma(hours: int, base: float = 4e-4) -> np.ndarray:
    minutes = np.arange(hours * 60)
    hod = (T0 // HOUR_S + minutes // 60) % 24
    return base * np.where((hod >= 13) & (hod < 21), 2.0, 1.0)  # US hours twice as volatile


def test_minute_returns_drop_gaps_and_sort() -> None:
    ts = np.array([180, 0, 60], dtype=np.int64)
    close = np.array([4.0, 1.0, 2.0])
    end, r = minute_returns(ts, close)
    assert end.tolist() == [60]
    assert r[0] == pytest.approx(math.log(2))
    with pytest.raises(ValueError, match="1-D"):
        minute_returns(ts, close[:2])


def test_hourly_rv_recovers_true_variance() -> None:
    ts, close = gbm(200, 4e-4)
    h = hourly_realized(ts, close)
    assert np.nanmean(h.rv) == pytest.approx(60 * 4e-4**2, rel=0.05)
    assert np.nanmean(h.bv) == pytest.approx(60 * 4e-4**2, rel=0.08)


def test_sparse_hours_are_nan() -> None:
    ts, close = gbm(3, 4e-4)
    keep = np.ones(ts.size, bool)
    keep[70:100] = False  # punch a 30-minute hole in hour 2
    h = hourly_realized(ts[keep], close[keep])
    assert np.isnan(h.rv[1])
    assert not np.isnan(h.rv[0])
    with pytest.raises(ValueError, match="no consecutive"):
        hourly_realized(ts[:1], close[:1])


def test_ewma_matches_constant_variance() -> None:
    rng = np.random.default_rng(1)
    r = rng.normal(0, 3e-4, 5000)
    assert ewma_var_per_s(r, halflife_min=500) == pytest.approx((3e-4) ** 2 / 60, rel=0.15)
    with pytest.raises(ValueError, match="at least one"):
        ewma_var_per_s(np.array([]))


def test_har_learns_seasonality_and_forecasts_level() -> None:
    hours = 24 * 40
    ts, close = gbm(hours, seasonal_sigma(hours))
    h = hourly_realized(ts, close)
    model = HarModel.fit(h)
    assert model.n_obs > 500
    busy = model.seasonal[15] / model.seasonal[3]
    assert busy == pytest.approx(4.0, rel=0.2)  # variance ratio = 2^2

    target = int(h.hours[-1]) + HOUR_S  # the hour after the data
    hod = (target // HOUR_S) % 24
    true_var = 60 * (4e-4 * (2.0 if 13 <= hod < 21 else 1.0)) ** 2
    assert model.hour_variance(h, target) == pytest.approx(true_var, rel=0.35)


def test_variance_between_spans_hours_and_scales_with_time() -> None:
    hours = 24 * 30
    ts, close = gbm(hours, seasonal_sigma(hours))
    h = hourly_realized(ts, close)
    model = HarModel.fit(h)
    start = int(h.hours[-1]) + HOUR_S
    one = model.variance_between(h, start, start + HOUR_S)
    half = model.variance_between(h, start, start + HOUR_S // 2)
    assert half == pytest.approx(one / 2)
    assert model.variance_between(h, start, start) == 0.0
    two = model.variance_between(h, start + HOUR_S // 2, start + HOUR_S + HOUR_S // 2)
    assert two > 0
    with pytest.raises(ValueError, match="follow"):
        model.hour_variance(h, int(h.hours[0]))


def test_har_needs_enough_history() -> None:
    ts, close = gbm(30, 4e-4)
    with pytest.raises(ValueError, match="not enough"):
        HarModel.fit(hourly_realized(ts, close))
