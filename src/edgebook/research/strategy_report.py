"""Paper-strategy analytics and the model-weight fit.

Two questions, answered from what the live engine records:

1. How did each paper strategy do, and would holding every entry to the result
   have done better than its actual exits? (``account_report``)
2. How much should fair value trust the model versus Kalshi's own price?
   (``fit_model_weight``) The engine logs model and market probabilities for
   every market it evaluates every 10 s, traded or not, so the fit is free of
   the selection bias that made day-one "edges" look real. The weight is chosen
   on the earlier part of the data and scored on the later part.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from edgebook.engine.decide import blend_fair
from edgebook.model.calibration import MeanCI, mean_ci

WEIGHTS = tuple(i / 20 for i in range(21))
MIN_MARKETS_TO_FIT = 40


# ---------------------------------------------------------------- decision log


def load_decisions(directory: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line from a restart
    return rows


def usable(rows: Iterable[dict[str, Any]], min_secs_left: float = 45.0) -> list[dict[str, Any]]:
    """Rows with both probabilities and live data, thinned to one per market per minute
    (10-second rows of one market are nearly copies of each other)."""
    seen: set[tuple[str, int]] = set()
    out = []
    for r in rows:
        if r.get("model_up") is None or r.get("market_up") is None or not r.get("gates_ok"):
            continue
        if (r.get("seconds_left") or 0) < min_secs_left:
            continue
        key = (r["ticker"], int(r["ts_ms"]) // 60_000)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _logloss(p: float, y: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


@dataclass(frozen=True)
class WeightFit:
    model_weight: float
    markets: int
    rows_train: int
    rows_test: int
    train_logloss: float
    test_logloss: float  # with the chosen weight
    test_logloss_market: float  # Kalshi's price alone (weight 0)
    test_logloss_model: float  # the model alone (weight 1)
    enough_data: bool


def fit_model_weight(rows: list[dict[str, Any]], results: dict[str, str]) -> WeightFit | None:
    """Pick the blend weight with the lowest log-loss on the first 70% of markets
    (by close order) and score it on the remaining 30%."""
    data = [
        (r, 1.0 if results[r["ticker"]] == "yes" else 0.0)
        for r in usable(rows)
        if results.get(r["ticker"]) in ("yes", "no")
    ]
    if not data:
        return None
    # split by market so one market's rows never sit on both sides
    first_seen: dict[str, int] = {}
    for r, _ in data:
        first_seen.setdefault(r["ticker"], int(r["ts_ms"]))
    order = sorted(first_seen, key=first_seen.__getitem__)
    cut = set(order[: max(1, int(len(order) * 0.7))])
    train = [(r, y) for r, y in data if r["ticker"] in cut]
    test = [(r, y) for r, y in data if r["ticker"] not in cut] or train

    def loss(sample: list[tuple[dict[str, Any], float]], w: float) -> float:
        return sum(
            _logloss(blend_fair(r["model_up"], r["market_up"], w), y) for r, y in sample
        ) / len(sample)

    best = min(WEIGHTS, key=lambda w: (loss(train, w), w))
    return WeightFit(
        model_weight=best,
        markets=len(order),
        rows_train=len(train),
        rows_test=len(test),
        train_logloss=loss(train, best),
        test_logloss=loss(test, best),
        test_logloss_market=loss(test, 0.0),
        test_logloss_model=loss(test, 1.0),
        enough_data=len(order) >= MIN_MARKETS_TO_FIT,
    )


# ---------------------------------------------------------------- accounts


@dataclass(frozen=True)
class AccountReport:
    name: str
    trades: int
    realized: float
    per_contract: MeanCI  # actual exits, $ per contract
    hold_per_contract: MeanCI  # had every entry been held to the result
    model_said: float | None  # average fair value of the side bought
    won: float | None
    paid: float | None


def round_trips(fills: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(entry, exit) pairs; partial maker entries on one market are merged."""
    out = []
    open_: dict[str, dict[str, Any]] = {}
    for f in fills:
        tk = f["ticker"]
        if f["action"] == "BUY":
            e = open_.get(tk)
            if e is None:
                open_[tk] = dict(f)
            else:
                n = e["contracts"] + f["contracts"]
                e["price"] = (e["price"] * e["contracts"] + f["price"] * f["contracts"]) / n
                e["fees"] += f["fees"]
                e["contracts"] = n
        elif tk in open_ and f["action"] in ("SELL", "SETTLE"):
            entry = open_[tk]
            sold = min(entry["contracts"], f["contracts"])
            fraction = sold / entry["contracts"]
            out.append(({**entry, "contracts": sold, "fees": entry["fees"] * fraction}, f))
            if sold >= entry["contracts"]:
                del open_[tk]
            else:
                open_[tk] = {
                    **entry,
                    "contracts": entry["contracts"] - sold,
                    "fees": entry["fees"] * (1 - fraction),
                }
    return out


def _fair_from_reason(reason: str) -> float | None:
    """'... a 64% chance' / 'Fair value 64% for UP' / 'fair value 0.64' -> 0.64."""
    m = re.search(r"(\d+)% chance|Fair value (\d+)%|fair value (0\.\d+)", reason)
    if not m:
        return None
    if m.group(3):
        return float(m.group(3))
    return int(m.group(1) or m.group(2)) / 100


def _avg(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def account_report(name: str, account: dict[str, Any], results: dict[str, str]) -> AccountReport:
    real, hold, said, won, paid = [], [], [], [], []
    for e, x in round_trips(account["fills"]):
        n = e["contracts"]
        if n <= 0:
            continue
        real.append((x["pnl"] or 0.0) / n)
        res = results.get(e["ticker"])
        if res in ("yes", "no"):
            w = 1.0 if res == e["side"] else 0.0
            hold.append(w - e["price"] - e["fees"] / n)
            won.append(w)
            paid.append(e["price"])
            fair = _fair_from_reason(e["reason"])
            if fair is not None:
                said.append(fair)
    return AccountReport(
        name=name,
        trades=len(real),
        realized=float(account.get("realized", 0.0)),
        per_contract=mean_ci(np.array(real, dtype=float)),
        hold_per_contract=mean_ci(np.array(hold, dtype=float)),
        model_said=_avg(said),
        won=_avg(won),
        paid=_avg(paid),
    )


def report_json(fit: WeightFit | None, accounts: list[AccountReport]) -> dict[str, Any]:
    return {
        "fit": asdict(fit) if fit else None,
        "accounts": [asdict(a) for a in accounts],
    }
