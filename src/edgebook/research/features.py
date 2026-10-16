"""Point-in-time feature matrix for (market, decision time) pairs.

Every feature uses only information available at the decision time
t = close - h: Kalshi candles that ended at or before t, spot closes at or
before t, and a HAR fit on hours before t's UTC day. Rows without a
tradeable quote, spot, or a fitted HAR are dropped (and counted).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from edgebook.history.kalshi import tradeable
from edgebook.model.pricer import contract_prob, prob_settle_above
from edgebook.model.vol import hourly_realized
from edgebook.research.calibrate import HarCache, Spot

F64 = npt.NDArray[np.float64]
EPS = 1e-4

FEATURES = (
    "logit_mid",
    "logit_model",
    "disagreement",  # logit_model - logit_mid
    "z_strike",  # log(spot/strike) in remaining-sigma units (signed toward YES)
    "spot_ret_1m",  # in per-minute sigma units
    "spot_ret_5m",
    "spot_ret_15m",
    "mid_chg_1m",
    "mid_chg_5m",
    "spread",
    "volume_5m_log",
    "horizon_min",
    "hour_sin",
    "hour_cos",
)


def logit(p: float) -> float:
    q = min(max(p, EPS), 1 - EPS)
    return math.log(q / (1 - q))


@dataclass
class FeatureSet:
    x: F64
    y: F64
    bid: F64
    ask: F64
    mid: F64
    model_p: F64
    day: npt.NDArray[np.int64]
    t: npt.NDArray[np.int64]
    horizon: npt.NDArray[np.int64]
    tickers: list[str]
    names: tuple[str, ...] = FEATURES
    skipped: dict[str, int] = field(default_factory=dict)


def _mid_at(candles: Sequence[dict[str, Any]], ts: int) -> float | None:
    best = None
    for c in candles:
        if c["end_ts"] > ts:
            break
        best = c
    if best is None or ts - best["end_ts"] > 300 or not tradeable(best["bid"], best["ask"]):
        return None
    return float(best["bid"] + best["ask"]) / 2


def _spot_ret(spot: Spot, t: int, s0: float, minutes: int, sig_min: float) -> float:
    """Spot log return over the last ``minutes``, in per-minute sigma units."""
    prev = spot.at(t - 60 * minutes)
    return 0.0 if prev is None else math.log(s0 / prev) / sig_min


def _mid_chg(cs: Sequence[dict[str, Any]], t: int, mid: float, minutes: int) -> float:
    prev = _mid_at(cs, t - 60 * minutes)
    return 0.0 if prev is None else mid - prev


def build(
    markets: Sequence[dict[str, Any]],
    candles: dict[str, list[dict[str, Any]]],
    spot_ts: npt.NDArray[np.int64],
    spot_close: F64,
    horizons: Sequence[int],
    nu: float | None = 4.0,
) -> FeatureSet:
    spot = Spot(spot_ts, spot_close)
    har = HarCache(hourly_realized(spot_ts, spot_close))
    var_cache: dict[tuple[int, int], float] = {}
    skipped: dict[str, int] = {}
    out: dict[str, list[Any]] = {
        k: [] for k in ("x", "y", "bid", "ask", "mid", "model_p", "day", "t", "h", "ticker")
    }

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for m in markets:
        cs = candles.get(m["ticker"], [])
        for h in horizons:
            t = int(m["close_ts"]) - 60 * h
            if t < m["open_ts"]:
                continue
            cur = None
            for c in cs:
                if c["end_ts"] > t:
                    break
                cur = c
            if cur is None or t - cur["end_ts"] > 300 or not tradeable(cur["bid"], cur["ask"]):
                skip("untradeable_quote")
                continue
            s0 = spot.at(t)
            model = har.model_for(t)
            if s0 is None:
                skip("no_spot")
                continue
            if model is None:
                skip("warmup")
                continue
            seconds = 60 * h
            if (t, seconds) not in var_cache:
                var_cache[(t, seconds)] = (
                    model.variance_between(har.hourly, t, t + seconds) / seconds
                )
            v = var_cache[(t, seconds)]
            floor, cap = m["floor"], m["cap"]
            p_floor = None if floor is None else prob_settle_above(s0, floor, seconds, v, nu=nu)
            p_cap = None if cap is None else prob_settle_above(s0, cap, seconds, v, nu=nu)
            p_model = min(max(contract_prob(m["strike_type"], floor, cap, p_floor, p_cap), 0), 1)

            bid, ask = float(cur["bid"]), float(cur["ask"])
            mid = (bid + ask) / 2
            sig_rem = math.sqrt(max(v * (seconds - 40), 1e-18))
            strike = floor if floor is not None else cap
            sign = -1.0 if m["strike_type"] in ("less", "less_or_equal") else 1.0
            z = sign * math.log(s0 / strike) / sig_rem if strike else 0.0
            sig_min = math.sqrt(v * 60)
            rets = [_spot_ret(spot, t, s0, minutes, sig_min) * sign for minutes in (1, 5, 15)]
            mid_chgs = [_mid_chg(cs, t, mid, minutes) for minutes in (1, 5)]
            vol5 = sum(c["volume"] for c in cs if t - 300 < c["end_ts"] <= t)
            hour = (t % 86_400) / 3600
            lm, lp = logit(mid), logit(p_model)
            out["x"].append(
                [
                    lm,
                    lp,
                    lp - lm,
                    z,
                    *rets,
                    *mid_chgs,
                    ask - bid,
                    math.log1p(vol5),
                    float(h),
                    math.sin(2 * math.pi * hour / 24),
                    math.cos(2 * math.pi * hour / 24),
                ]
            )
            out["y"].append(1.0 if m["result"] == "yes" else 0.0)
            out["bid"].append(bid)
            out["ask"].append(ask)
            out["mid"].append(mid)
            out["model_p"].append(p_model)
            out["day"].append(t // 86_400)
            out["t"].append(t)
            out["h"].append(h)
            out["ticker"].append(m["ticker"])

    n_feat = len(FEATURES)
    return FeatureSet(
        x=np.array(out["x"], dtype=np.float64).reshape(-1, n_feat),
        y=np.array(out["y"], dtype=np.float64),
        bid=np.array(out["bid"], dtype=np.float64),
        ask=np.array(out["ask"], dtype=np.float64),
        mid=np.array(out["mid"], dtype=np.float64),
        model_p=np.array(out["model_p"], dtype=np.float64),
        day=np.array(out["day"], dtype=np.int64),
        t=np.array(out["t"], dtype=np.int64),
        horizon=np.array(out["h"], dtype=np.int64),
        tickers=out["ticker"],
        skipped=skipped,
    )
