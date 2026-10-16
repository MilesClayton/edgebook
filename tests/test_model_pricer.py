import math

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from edgebook.model.pricer import contract_prob, prob_settle_above

SPOT = 82_000.0
V = (0.5 / math.sqrt(365 * 24 * 3600)) ** 2  # 50% annualized vol, per second

strikes = st.floats(60_000, 110_000)
horizons = st.floats(60, 7200)
nus = st.one_of(st.none(), st.floats(2.5, 30))


@given(strikes, strikes, horizons, nus)
def test_bounded_and_decreasing_in_strike(k1: float, k2: float, t: float, nu: float | None) -> None:
    lo, hi = sorted((k1, k2))
    p_lo = prob_settle_above(SPOT, lo, t, V, nu=nu)
    p_hi = prob_settle_above(SPOT, hi, t, V, nu=nu)
    assert 0.0 <= p_hi <= p_lo <= 1.0


@given(horizons, nus)
def test_at_the_money_is_near_half(t: float, nu: float | None) -> None:
    p = prob_settle_above(SPOT, SPOT, t, V, nu=nu)
    assert 0.45 < p <= 0.5  # slightly under 1/2: lognormal median sits below the mean


def test_matches_monte_carlo_including_window_average() -> None:
    rng = np.random.default_rng(0)
    t, strike, v = 600.0, 82_150.0, V * 25  # high vol so the window matters
    n = 40_000
    pre = rng.normal(0, math.sqrt(v * (t - 60)), n)
    steps = rng.normal(0, math.sqrt(v), (n, 60)).cumsum(axis=1)
    paths = SPOT * np.exp(pre[:, None] + steps - v * np.arange(1, 61) / 2 - v * (t - 60) / 2)
    mc = float((paths.mean(axis=1) > strike).mean())
    assert prob_settle_above(SPOT, strike, t, v) == pytest.approx(mc, abs=0.01)


def test_fat_tails_raise_far_otm_probability() -> None:
    far = SPOT * 1.03
    assert prob_settle_above(SPOT, far, 3600, V, nu=4) > prob_settle_above(SPOT, far, 3600, V)


def test_inside_window_uses_known_average() -> None:
    # 50s already printed at 83k; 10s left near 82k. Average ~ 82,833: above 82.5k, below 83k.
    kw = {"known_avg": 83_000.0}
    assert prob_settle_above(SPOT, 82_500, 10, V, **kw) > 0.99
    assert prob_settle_above(SPOT, 83_000, 10, V, **kw) < 0.01
    assert prob_settle_above(SPOT, 82_500, 0, V, known_avg=82_600.0) == 1.0
    assert prob_settle_above(SPOT, 1.0, 30, V, known_avg=90_000.0) == 1.0
    with pytest.raises(ValueError, match="known_avg"):
        prob_settle_above(SPOT, SPOT, 30, V)


def test_zero_variance_is_a_step() -> None:
    assert prob_settle_above(SPOT, SPOT - 1, 600, 0.0) == 1.0
    assert prob_settle_above(SPOT, SPOT + 1, 600, 0.0) == 0.0


@pytest.mark.parametrize(
    ("args", "msg"),
    [
        ((0, 1, 60, V), "positive"),
        ((1, 1, -1, V), "non-negative"),
        ((1, 1, 60, -V), "non-negative"),
    ],
)
def test_rejects_bad_inputs(args: tuple[float, float, float, float], msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        prob_settle_above(*args)
    with pytest.raises(ValueError, match="nu"):
        prob_settle_above(SPOT, SPOT, 600, V, nu=2)


def test_contract_types() -> None:
    assert contract_prob("greater", 80_000, None, p_above=0.7) == 0.7
    assert contract_prob("less", None, 84_000, p_above_cap=0.2) == pytest.approx(0.8)
    assert contract_prob("between", 80_000, 84_000, 0.7, 0.2) == pytest.approx(0.5)
    for bad in [
        ("greater", None, None),
        ("less", None, None),
        ("between", 1, None),
        ("custom", 1, 2),
    ]:
        with pytest.raises(ValueError, match=r"needs|unsupported"):
            contract_prob(*bad)
