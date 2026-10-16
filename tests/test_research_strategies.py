"""Strategy search on a synthetic world where the truth is known."""

import math
from typing import Any

import numpy as np
import pytest

from edgebook.research import features, strategies
from edgebook.research.calibrate import WARMUP_DAYS

DAY = 86_400
T0 = 1_790_000_000 // DAY * DAY
SIG = 4e-4


def world(days: int, market_noise: float, seed: int = 0) -> tuple[Any, ...]:
    """GBM spot; 15-min up/down markets whose quotes are truth + noise."""
    rng = np.random.default_rng(seed)
    n = days * 1440 + 1
    close = 80_000 * np.exp(np.concatenate([[0.0], np.cumsum(rng.normal(0, SIG, n - 1))]))
    ts = T0 + 60 * np.arange(n, dtype=np.int64)
    markets, candles = [], {}
    for k in range((WARMUP_DAYS + 1) * 96, days * 96 - 1, 2):
        open_ts, close_ts = T0 + k * 900, T0 + (k + 1) * 900
        strike = float(close[(open_ts - T0) // 60])
        settle = float(close[(close_ts - T0) // 60])
        tk = f"M{k}"
        markets.append(
            {
                "ticker": tk,
                "open_ts": open_ts,
                "close_ts": close_ts,
                "strike_type": "greater_or_equal",
                "floor": strike,
                "cap": None,
                "result": "yes" if settle >= strike else "no",
            }
        )
        cs = []
        for m in range(15):
            t = open_ts + 60 * (m + 1)
            left = close_ts - t
            spot = float(close[(t - T0) // 60])
            sd = SIG * math.sqrt(max(left / 60, 1e-9))
            true_p = 0.5 * math.erfc(-math.log(spot / strike) / sd / math.sqrt(2))
            mid = min(max(true_p + rng.normal(0, market_noise), 0.04), 0.96)
            cs.append(
                {
                    "ticker": tk,
                    "end_ts": t,
                    "bid": round(mid - 0.01, 2),
                    "ask": round(mid + 0.01, 2),
                    "last": None,
                    "volume": 3.0,
                }
            )
        candles[tk] = cs
    return ts, close, markets, candles


def test_features_are_point_in_time_and_shaped() -> None:
    ts, close, markets, candles = world(days=12, market_noise=0.0)
    fs = features.build(markets, candles, ts, close, horizons=(10, 5))
    assert fs.x.shape == (fs.y.size, len(features.FEATURES))
    assert fs.y.size > 100
    assert np.all(fs.t <= np.array([0] * fs.t.size) + fs.t)  # sanity: populated
    assert set(np.unique(fs.horizon)) == {10, 5}
    assert np.isfinite(fs.x).all()


def test_noisy_market_is_beaten_and_clean_market_is_not() -> None:
    ts, close, markets, candles = world(days=20, market_noise=0.12)
    fs = features.build(markets, candles, ts, close, horizons=(10, 5))
    res = {r.name: r for r in strategies.run(fs, ["market", "model_t4", "blend"])}
    assert res["market"].all_oos.taker[0.02].n == 0  # the market never disagrees with itself
    assert res["model_t4"].all_oos.brier < res["model_t4"].all_oos.market_brier
    assert res["model_t4"].all_oos.taker[0.02].mean > 0
    assert res["model_t4"].equity[-1][1] > 0
    text = strategies.render("SYN", fs, list(res.values()))
    assert "holdout" in text


def test_walk_forward_never_trains_on_test_day() -> None:
    ts, close, markets, candles = world(days=20, market_noise=0.05)
    fs = features.build(markets, candles, ts, close, horizons=(5,))
    seen: list[tuple[int, int]] = []

    def spy(x: Any, y: Any) -> Any:
        seen.append((len(y), int(fs.day[: len(y)].max())))
        return lambda xt: np.full(len(xt), 0.5)

    p = strategies.walk_forward(fs, spy)
    first_test_day = np.unique(fs.day)[strategies.MIN_TRAIN_DAYS]
    assert np.isnan(p[fs.day < first_test_day]).all()
    assert not np.isnan(p[fs.day >= first_test_day]).any()
    assert len(seen) == len(np.unique(fs.day)) - strategies.MIN_TRAIN_DAYS


def test_price_bias_sides_are_distinct_and_fee_aware() -> None:
    assert strategies.buy_yes_pnl(0.50, 1) == pytest.approx(1 - 0.50 - 0.02)
    assert strategies.buy_no_pnl(0.50, 0) == pytest.approx(1 - 0.50 - 0.02)
    assert strategies.buy_no_pnl(0.90, 1) == pytest.approx(-0.10 - 0.01)
    ts, close, markets, candles = world(days=12, market_noise=0.0)
    fs = features.build(markets, candles, ts, close, horizons=(5,))
    buckets = strategies.price_bias(fs)
    assert sum(b.n for b in buckets) == fs.y.size
    assert any(b.yes.mean != b.no.mean for b in buckets if b.n > 1)
    assert "buy NO" in strategies.render_bias(buckets)
