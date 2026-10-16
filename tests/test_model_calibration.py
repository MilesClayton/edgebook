import numpy as np
import pytest

from edgebook.model.calibration import brier, log_loss, mean_ci, reliability, wilson


def test_scores_reward_calibrated_forecasts() -> None:
    rng = np.random.default_rng(0)
    p = rng.uniform(0, 1, 20_000)
    y = (rng.uniform(0, 1, p.size) < p).astype(np.float64)
    flat = np.full(p.size, 0.5)
    assert brier(p, y) < brier(flat, y)
    assert log_loss(p, y) < log_loss(flat, y)
    buckets = reliability(p, y)
    assert len(buckets) == 10
    assert sum(b.n for b in buckets) == p.size
    assert sum(b.calibrated for b in buckets) >= 9


def test_overconfident_forecast_is_flagged() -> None:
    rng = np.random.default_rng(1)
    y = (rng.uniform(0, 1, 5000) < 0.6).astype(np.float64)
    buckets = reliability(np.full(y.size, 0.95), y)
    assert not buckets[-1].calibrated
    assert buckets[0].n == 0
    assert buckets[0].calibrated


def test_wilson_and_mean_ci() -> None:
    lo, hi = wilson(50, 100)
    assert lo < 0.5 < hi
    assert wilson(0, 0) == (0.0, 1.0)
    m = mean_ci(np.array([1.0, 2.0, 3.0]))
    assert m.mean == 2.0
    assert m.ci_lo < 2.0 < m.ci_hi
    assert mean_ci(np.array([])).n == 0
    assert np.isnan(mean_ci(np.array([5.0])).ci_lo)


def test_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="1-D"):
        brier(np.array([0.5]), np.array([1.0, 0.0]))
    with pytest.raises(ValueError, match="0/1"):
        brier(np.array([0.5]), np.array([0.5]))
