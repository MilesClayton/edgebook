"""Probability that a Kalshi crypto contract settles YES.

Settlement is the simple average of the 60 one-second index prints before
close. With log price following driftless diffusion at variance ``v`` per
second:

- from now until the averaging window opens, log price picks up ``v * pre``
- the window average of a Brownian path over ``w`` seconds has variance
  ``v * w / 3`` around the price at window open

so log(settlement) ~ log(spot) - s^2/2 + s * eps with s^2 = v * (pre + w/3).
Inside the window, the already-printed part of the average is known and only
the remainder is random. ``nu`` switches eps from normal to a unit-variance
Student-t for fat tails.
"""

from __future__ import annotations

import math

from scipy import stats

WINDOW_S = 60


def _sf(z: float, nu: float | None) -> float:
    if nu is None:
        return float(stats.norm.sf(z))
    if nu <= 2:
        raise ValueError("nu must exceed 2 for a finite-variance Student-t")
    return float(stats.t.sf(z / math.sqrt((nu - 2) / nu), nu))


def prob_settle_above(
    spot: float,
    strike: float,
    seconds_to_close: float,
    var_per_s: float,
    *,
    window_s: float = WINDOW_S,
    known_avg: float | None = None,
    nu: float | None = None,
) -> float:
    """P(60s-average settlement > strike).

    ``known_avg`` is the average of index prints already inside the window
    (required when ``seconds_to_close < window_s``).
    """
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if var_per_s < 0 or seconds_to_close < 0:
        raise ValueError("variance and time must be non-negative")

    if seconds_to_close >= window_s:
        s2 = var_per_s * ((seconds_to_close - window_s) + window_s / 3)
        threshold, center = strike, spot
    else:
        if known_avg is None:
            raise ValueError("inside the averaging window: known_avg is required")
        remaining = seconds_to_close
        w_known = (window_s - remaining) / window_s
        if remaining == 0:
            return 1.0 if known_avg > strike else 0.0
        # Settlement = w_known*known_avg + (1-w_known)*future_avg; need future_avg > threshold.
        threshold = (strike - w_known * known_avg) / (1 - w_known)
        if threshold <= 0:
            return 1.0
        s2 = var_per_s * remaining / 3
        center = spot

    if s2 <= 0:
        return 1.0 if center > threshold else 0.0
    s = math.sqrt(s2)
    z = (math.log(threshold / center) + s2 / 2) / s
    return _sf(z, nu)


def contract_prob(
    strike_type: str,
    floor: float | None,
    cap: float | None,
    p_above: float | None = None,
    p_above_cap: float | None = None,
) -> float:
    """Map P(settle > floor) and P(settle > cap) onto Kalshi strike types."""
    if strike_type in ("greater", "greater_or_equal"):
        if floor is None or p_above is None:
            raise ValueError(f"{strike_type} needs floor and p_above")
        return p_above
    if strike_type in ("less", "less_or_equal"):
        if cap is None or p_above_cap is None:
            raise ValueError(f"{strike_type} needs cap and p_above_cap")
        return 1.0 - p_above_cap
    if strike_type == "between":
        if floor is None or cap is None or p_above is None or p_above_cap is None:
            raise ValueError("between needs floor, cap and both probabilities")
        return max(0.0, p_above - p_above_cap)
    raise ValueError(f"unsupported strike_type {strike_type!r}")
