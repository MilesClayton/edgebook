"""Where does the long-shot seller lose? An ML gap analysis on the 1-minute backfill.

The live long-shot pilot rests a bid on the favourite whenever a side is offered at
2-15c with 5-60 min left (``engine.maker.longshot_quote``) and holds to the result. This
module rebuilds every moment that rule would have fired, attaches point-in-time features,
and asks two questions:

1. **Gaps:** which conditions (time left, price, distance to strike, momentum, volatility,
   series, hour) concentrate the losses?
2. **Filter:** can a model trained on earlier closes pick out sales to skip, so the sales
   it keeps earn more on later closes it never saw?

Two things keep the evidence honest:

- *Adverse selection.* A resting sale only fills when someone buys the long shot, which is
  likelier when price is moving toward it. Every result is reported for all firings and
  for a "would have filled" subset (a trade at or through our price within
  ``FILL_WINDOW_MIN`` minutes, from the candles' last/bid/ask).
- *Dependence.* One market appears at many horizons and all strikes of one close move
  together. Splits are chronological by close time, policies take one entry per market,
  and confidence intervals resample whole closes.

Labels are Kalshi's official results; Coinbase spot is used only for features.
"""

from __future__ import annotations

import bisect
import itertools
import json
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from scipy.stats import norm
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from edgebook.core.types import LiquidityRole
from edgebook.engine.decide import fee
from edgebook.engine.maker import LongshotConfig

HORIZONS_MIN = tuple(range(60, 4, -5))  # 60, 55, ..., 5 minutes before close
# The rule as it ran when this study was made (selling down to 5 minutes before close). The
# study keeps that wider scope so the final-15-minute gap it found stays reproducible.
STUDY_CONFIG = LongshotConfig(min_secs_left=300.0)
FILL_WINDOW_MIN = 10
PAPER_MODEL_WEIGHT = 0.2  # the paper strategy blends 20% model into the market price
FEATURES = (
    "hourly",
    "minutes_left",
    "long_ask",
    "spread",
    "long_is_up",
    "z_to_strike",
    "model_p_long",
    "model_minus_ask",
    "mom_5m",
    "mom_15m",
    "vol_60m_bps",
    "vol_ratio",
    "ask_change_5m",
    "volume_15m",
    "hour_utc",
    "weekend",
)


@dataclass(frozen=True)
class Firing:
    """One moment the long-shot rule fires on one market."""

    event: str
    ticker: str
    close_ts: int
    minutes_left: int
    long_ask: float  # price the long shot is offered at; we sell it there
    won_long: bool  # official result: the long shot paid out
    filled: bool  # a trade reached our price within FILL_WINDOW_MIN minutes
    pnl: float  # $ per contract to the seller if filled: kept premium - favourite cost - fee
    features: dict[str, float]


def seller_pnl(long_ask: float, won_long: bool) -> float:
    """Selling a long shot at ``long_ask`` = buying the favourite at 1 - long_ask as maker."""
    q = round(1 - long_ask, 2)
    return (0.0 if won_long else 1.0) - q - fee(q, LiquidityRole.MAKER)


class Spot:
    def __init__(self, ts: Sequence[int], close: Sequence[float]) -> None:
        order = np.argsort(np.asarray(ts))
        self.ts = np.asarray(ts, dtype=np.int64)[order]
        self.logp = np.log(np.asarray(close, dtype=float)[order])

    def at(self, t: int) -> int | None:
        i = int(np.searchsorted(self.ts, t, side="right")) - 1
        return i if i >= 0 and t - int(self.ts[i]) <= 120 else None

    def vol(self, i: int, n: int) -> float | None:
        """Per-minute log-return standard deviation over the previous n minutes."""
        if i < n:
            return None
        r = np.diff(self.logp[i - n : i + 1])
        s = float(r.std(ddof=1))
        return s if s > 0 else None


def _quote(rows: list[tuple[int, float, float, float, float]], t: int) -> int | None:
    ends = [r[0] for r in rows]
    i = bisect.bisect_right(ends, t) - 1
    if i < 0 or t - rows[i][0] > 120:
        return None
    _, bid, ask, _, _ = rows[i]
    return i if bid is not None and ask is not None and 0 < bid < ask < 1 else None


def _long_shot(bid: float, ask: float, cfg: LongshotConfig) -> tuple[bool, float] | None:
    """(long shot is UP, its ask) under the paper rule, which checks UP first."""
    if cfg.min_ask <= ask <= cfg.max_ask:
        return True, ask
    if cfg.min_ask <= round(1 - bid, 4) <= cfg.max_ask:
        return False, round(1 - bid, 4)
    return None


def _filled(
    rows: list[tuple[int, float, float, float, float]], i: int, up: bool, long_ask: float
) -> bool:
    """Did a trade reach our resting price in the next FILL_WINDOW_MIN minutes?

    Selling UP at a = a YES ask at a: filled when YES trades at >= a or the bid reaches a.
    Selling DOWN at a = a YES bid at 1 - a: filled when YES trades at <= 1 - a or the ask
    falls to it. ``last`` only counts in candles with volume.
    """
    start = rows[i][0]
    yes_px = long_ask if up else 1 - long_ask
    for end, bid, ask, last, volume in rows[i + 1 :]:
        if end - start > FILL_WINDOW_MIN * 60:
            break
        traded = volume is not None and volume > 0 and last is not None
        if up and ((traded and last >= yes_px - 1e-9) or (bid is not None and bid >= yes_px)):
            return True
        if not up and ((traded and last <= yes_px + 1e-9) or (ask is not None and ask <= yes_px)):
            return True
    return False


def firings(history_dir: Path, cfg: LongshotConfig | None = None) -> list[Firing]:
    """Every (market, horizon) where the paper long-shot rule would rest a sale."""
    cfg = cfg or LongshotConfig()
    spot_t = pq.read_table(history_dir / "spot_1m_BTC-USD.parquet").to_pydict()
    spot = Spot(spot_t["ts"], spot_t["close"])
    out: list[Firing] = []
    for series in ("KXBTC15M", "KXBTCD"):
        markets = pq.read_table(history_dir / f"kalshi_markets_{series}.parquet").to_pylist()
        candles = pq.read_table(
            history_dir / f"kalshi_candles_{series}.parquet",
            columns=["ticker", "end_ts", "bid", "ask", "last", "volume"],
        ).to_pydict()
        by_ticker: dict[str, list[tuple[int, float, float, float, float]]] = defaultdict(list)
        for tk, end, bid, ask, last, vol in zip(
            candles["ticker"],
            candles["end_ts"],
            candles["bid"],
            candles["ask"],
            candles["last"],
            candles["volume"],
            strict=True,
        ):
            by_ticker[tk].append((end, bid, ask, last, vol))
        for rows in by_ticker.values():
            rows.sort(key=lambda r: r[0])
        for m in markets:
            if m["result"] not in ("yes", "no") or m["floor"] is None:
                continue
            rows = by_ticker.get(m["ticker"], [])
            for h in HORIZONS_MIN:
                if not cfg.min_secs_left <= h * 60 <= cfg.max_secs_left:
                    continue
                t = m["close_ts"] - h * 60
                if t < m["open_ts"]:
                    continue
                i = _quote(rows, t)
                if i is None:
                    continue
                _, bid, ask, _, _ = rows[i]
                shot = _long_shot(bid, ask, cfg)
                si = spot.at(t)
                if shot is None or si is None:
                    continue
                up, long_ask = shot
                feats = _features(m, rows, i, h, up, long_ask, bid, ask, spot, si, series)
                if feats is None:
                    continue
                won = (m["result"] == "yes") == up
                out.append(
                    Firing(
                        m["event_ticker"],
                        m["ticker"],
                        m["close_ts"],
                        h,
                        long_ask,
                        won,
                        _filled(rows, i, up, long_ask),
                        seller_pnl(long_ask, won),
                        feats,
                    )
                )
    return out


def _features(
    m: dict[str, Any],
    rows: list[tuple[int, float, float, float, float]],
    i: int,
    h: int,
    up: bool,
    long_ask: float,
    bid: float,
    ask: float,
    spot: Spot,
    si: int,
    series: str,
) -> dict[str, float] | None:
    vol60 = spot.vol(si, 60)
    vol1d = spot.vol(si, 1440)
    if vol60 is None or vol1d is None or si < 15:
        return None
    sign = 1.0 if up else -1.0  # positive = toward the long shot paying out
    # How far price must travel, in log terms, for the long shot to win
    gap = sign * (math.log(m["floor"]) - float(spot.logp[si]))
    z = gap / (vol60 * math.sqrt(h))
    model_p = float(norm.sf(z))
    t = rows[i][0]
    prev = _quote(rows, t - 300)
    prev_long = None
    if prev is not None:
        _, pb, pa, _, _ = rows[prev]
        prev_long = pa if up else 1 - pb
    vol15 = sum(r[4] or 0.0 for r in rows[max(0, i - 15) : i + 1] if t - r[0] < 900)
    dt = datetime.fromtimestamp(t, tz=UTC)
    return {
        "hourly": 1.0 if series == "KXBTCD" else 0.0,
        "minutes_left": float(h),
        "long_ask": long_ask,
        "spread": ask - bid,
        "long_is_up": 1.0 if up else 0.0,
        "z_to_strike": z,
        "model_p_long": model_p,
        "model_minus_ask": model_p - long_ask,
        "mom_5m": sign * float(spot.logp[si] - spot.logp[si - 5]) * 1e4,
        "mom_15m": sign * float(spot.logp[si] - spot.logp[si - 15]) * 1e4,
        "vol_60m_bps": vol60 * 1e4,
        "vol_ratio": vol60 / vol1d,
        "ask_change_5m": long_ask - prev_long if prev_long is not None else 0.0,
        "volume_15m": math.log1p(vol15),
        "hour_utc": float(dt.hour),
        "weekend": 1.0 if dt.weekday() >= 5 else 0.0,
    }


# ------------------------------------------------------------------ evaluation helpers


def event_ci(
    pnl: np.ndarray, events: np.ndarray, n_boot: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """95% CI of the per-contract mean, resampling whole closes (events)."""
    if len(pnl) == 0:
        return (math.nan, math.nan)
    uniq, inv = np.unique(events, return_inverse=True)
    sums = np.bincount(inv, weights=pnl)
    counts = np.bincount(inv)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
    means = sums[idx].sum(axis=1) / np.maximum(counts[idx].sum(axis=1), 1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def summary(rows: Sequence[Firing]) -> dict[str, Any]:
    pnl = np.array([r.pnl for r in rows])
    ev = np.array([r.event for r in rows])
    lo, hi = event_ci(pnl, ev)
    return {
        "n": len(rows),
        "closes": len(set(ev.tolist())),
        "mean_cents": float(pnl.mean() * 100) if len(pnl) else None,
        "ci_cents": [lo * 100, hi * 100],
        "long_shot_win_rate": float(np.mean([r.won_long for r in rows])) if rows else None,
        "avg_ask": float(np.mean([r.long_ask for r in rows])) if rows else None,
    }


def first_per_market(rows: Sequence[Firing], keep: np.ndarray | None = None) -> list[Firing]:
    """The paper strategy holds to the result: one entry per market, at the earliest firing
    that a policy keeps (and that would have filled)."""
    chosen: dict[str, Firing] = {}
    for j, r in enumerate(rows):
        if keep is not None and not keep[j]:
            continue
        if not r.filled:
            continue
        best = chosen.get(r.ticker)
        if best is None or r.minutes_left > best.minutes_left:
            chosen[r.ticker] = r
    return list(chosen.values())


def chrono_split(rows: Sequence[Firing]) -> tuple[list[int], list[int], list[int]]:
    """Train / validation / test = first 60% / next 20% / last 20% of closes by time."""
    closes = sorted({(r.close_ts, r.event) for r in rows})
    a, b = int(len(closes) * 0.6), int(len(closes) * 0.8)
    train_ev = {e for _, e in closes[:a]}
    val_ev = {e for _, e in closes[a:b]}
    tr: list[int] = []
    va: list[int] = []
    te: list[int] = []
    for j, r in enumerate(rows):
        (tr if r.event in train_ev else va if r.event in val_ev else te).append(j)
    return tr, va, te


BUCKETS: dict[str, tuple[float, ...]] = {
    "minutes_left": (5, 15, 30, 45, 61),
    "long_ask": (0.02, 0.04, 0.07, 0.10, 0.151),
    "z_to_strike": (-10, 1.0, 1.5, 2.0, 3.0, 99),
    "mom_15m": (-1e9, -10, 0, 10, 1e9),
    "vol_ratio": (0, 0.7, 1.0, 1.4, 99),
    "model_minus_ask": (-1, -0.05, -0.02, 0, 1),
    "ask_change_5m": (-1, -0.005, 0.005, 1),
    "hourly": (0, 0.5, 1.5),
}


def gap_table(rows: Sequence[Firing]) -> dict[str, list[dict[str, Any]]]:
    """Seller P&L by condition, on filled firings: where the losses concentrate."""
    out: dict[str, list[dict[str, Any]]] = {}
    for name, edges in BUCKETS.items():
        table = []
        for lo, hi in itertools.pairwise(edges):
            sel = [r for r in rows if r.filled and lo <= r.features[name] < hi]
            if len(sel) >= 30:
                table.append({"bucket": f"[{lo:g}, {hi:g})", **summary(sel)})
        out[name] = table
    return out


# ------------------------------------------------------------------ models


@dataclass
class ModelReport:
    name: str
    brier: float
    log_loss: float
    brier_market: float
    log_loss_market: float


def _matrix(rows: Sequence[Firing], idx: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
    x = np.array([[rows[j].features[f] for f in FEATURES] for j in idx], dtype=float)
    y = np.array([rows[j].won_long for j in idx], dtype=int)
    return x, y


def analyse(history_dir: Path, seed: int = 0) -> dict[str, Any]:
    rows = firings(history_dir, STUDY_CONFIG)
    tr, va, te = chrono_split(rows)
    x_tr, y_tr = _matrix(rows, tr)
    x_va, y_va = _matrix(rows, va)
    x_te, y_te = _matrix(rows, te)
    ask_te = np.array([rows[j].long_ask for j in te])
    ask_va = np.array([rows[j].long_ask for j in va])

    gbm = HistGradientBoostingClassifier(
        max_iter=300,
        learning_rate=0.05,
        max_leaf_nodes=15,
        min_samples_leaf=200,
        l2_regularization=1.0,
        random_state=seed,
    ).fit(x_tr, y_tr)
    logit = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=2000)).fit(
        x_tr, y_tr
    )
    models: dict[str, Any] = {"gbm": gbm, "logistic": logit}
    calibrated: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    reports = []
    for name, model in models.items():
        iso = IsotonicRegression(out_of_bounds="clip", y_min=1e-4, y_max=1 - 1e-4)
        iso.fit(model.predict_proba(x_va)[:, 1], y_va)
        p_va = iso.predict(model.predict_proba(x_va)[:, 1])
        p_te = iso.predict(model.predict_proba(x_te)[:, 1])
        calibrated[name] = (p_va, p_te)
        reports.append(
            ModelReport(
                name,
                float(brier_score_loss(y_te, p_te)),
                float(log_loss(y_te, np.clip(p_te, 1e-4, 1 - 1e-4))),
                float(brier_score_loss(y_te, ask_te)),
                float(log_loss(y_te, np.clip(ask_te, 1e-4, 1 - 1e-4))),
            )
        )

    def expected_pnl(p: np.ndarray, ask: np.ndarray) -> np.ndarray:
        q = np.round(1 - ask, 2)
        fees = np.array([fee(float(v), LiquidityRole.MAKER) for v in q])
        return np.asarray((1 - p) - q - fees, dtype=float)

    # The paper rule (what runs live): skip only when blended fair value says the long shot
    # is worth its ask. Its 20%-model blend is approximated with the driftless vol model.
    def paper_keep(idx: Sequence[int]) -> np.ndarray:
        out = []
        for j in idx:
            f = rows[j].features
            mid = rows[j].long_ask - f["spread"] / 2
            fair = PAPER_MODEL_WEIGHT * f["model_p_long"] + (1 - PAPER_MODEL_WEIGHT) * mid
            out.append(fair < rows[j].long_ask)
        return np.array(out)

    test_rows = [rows[j] for j in te]
    policies: dict[str, dict[str, Any]] = {
        "sell_every_firing": summary(first_per_market(test_rows)),
        "paper_rule": summary(first_per_market(test_rows, paper_keep(te))),
    }
    thresholds: dict[str, float] = {}
    for name, (p_va, p_te) in calibrated.items():
        # pick the expected-P&L cut-off on validation only, then apply it once to test
        val_rows = [rows[j] for j in va]
        e_va = expected_pnl(p_va, ask_va)
        best_cut, best_mean = 0.0, -math.inf
        for cut in (-0.02, -0.01, 0.0, 0.005, 0.01, 0.015, 0.02, 0.03):
            kept = first_per_market(val_rows, e_va > cut)
            if len(kept) < 0.25 * len(first_per_market(val_rows)):
                continue
            mean = float(np.mean([r.pnl for r in kept]))
            if mean > best_mean:
                best_cut, best_mean = cut, mean
        thresholds[name] = best_cut
        keep = expected_pnl(p_te, ask_te) > best_cut
        policies[f"{name}_filter"] = summary(first_per_market(test_rows, keep))
        skipped = [r for r, k in zip(test_rows, keep, strict=True) if not k]
        policies[f"{name}_skipped"] = summary(first_per_market(skipped))

    imp = permutation_importance(
        gbm, x_te, y_te, scoring="neg_brier_score", n_repeats=5, random_state=seed
    )
    gains = sorted(
        zip(FEATURES, imp.importances_mean, imp.importances_std, strict=True),
        key=lambda t: -float(t[1]),
    )
    importance = [{"feature": f, "brier_gain": float(m), "sd": float(s)} for f, m, s in gains]
    explore = [rows[j] for j in tr + va]
    return {
        "data": {
            "firings": len(rows),
            "filled_share": float(np.mean([r.filled for r in rows])),
            "markets": len({r.ticker for r in rows}),
            "closes": len({r.event for r in rows}),
            "first_close": datetime.fromtimestamp(min(r.close_ts for r in rows), UTC).isoformat(),
            "last_close": datetime.fromtimestamp(max(r.close_ts for r in rows), UTC).isoformat(),
            "split_closes": [len({rows[j].event for j in s}) for s in (tr, va, te)],
        },
        "adverse_selection": {
            "all_firings": summary(rows),
            "filled_only": summary([r for r in rows if r.filled]),
            "unfilled_only": summary([r for r in rows if not r.filled]),
        },
        "gaps_train_val": gap_table(explore),
        "gaps_test": gap_table(test_rows),
        "models": [asdict(r) for r in reports],
        "thresholds_from_validation": thresholds,
        "test_policies_one_entry_per_market": policies,
        "gbm_permutation_importance_test": importance,
    }


def main() -> None:  # pragma: no cover - research entry point
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history", type=Path, default=Path("history"))
    parser.add_argument("--out", type=Path, default=Path("reports/longshot-gaps.json"))
    args = parser.parse_args()
    result = analyse(args.history)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
