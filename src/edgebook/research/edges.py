"""Edges from the open-source bot survey, tested on 1-minute history.

Every rule is applied exactly as published (thresholds from the source repos)
so nothing here is tuned on our data; the last ``HOLDOUT_DAYS`` are reported
separately anyway. Taker fills at minute-close quotes; both legs pay fees.

1. ladder_arb      structural: YES(K1) + NO(K2) < $1 after fees, K1 < K2 (hourly)
2. early_leader    J0shusmc: minute 2-3, side ask 56-60c within 10c of its peak
3. reclaim         J0shusmc: post-sweep reclaim lanes, targets 70c / 80c
4. expiry_decay    ryanathompson: 2-3 min left, z >= 2, model - ask >= 3c, ask 40-75c

Minute-close quotes are not guaranteed to be simultaneously executable, so
arb counts are an upper bound on what a live bot could take.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from edgebook.model.calibration import MeanCI, mean_ci
from edgebook.research.calibrate import Spot
from edgebook.research.paths import fee

HOLDOUT_DAYS = 7
Candle = dict[str, Any]
MarketRow = dict[str, Any]


@dataclass(frozen=True)
class EdgeTrade:
    edge: str
    ticker: str
    ts: int
    side: str
    entry: float
    pnl: float


@dataclass(frozen=True)
class EdgeResult:
    edge: str
    train: MeanCI
    holdout: MeanCI
    note: str = ""


def _side_px(c: Candle, side: str) -> tuple[float, float]:
    """(ask, bid) for the side held."""
    return (c["ask"], c["bid"]) if side == "yes" else (1 - c["bid"], 1 - c["ask"])


def _settle(m: MarketRow, side: str) -> float:
    won = (m["result"] == "yes") == (side == "yes")
    return 1.0 if won else 0.0


def _exit_at_target(
    cs: Sequence[Candle], start: int, side: str, target: float, m: MarketRow
) -> float:
    """Sell at the first minute the side's bid reaches ``target``, else settle."""
    for c in cs[start:]:
        _, bid = _side_px(c, side)
        if bid >= target:
            return bid - fee(bid)
    return _settle(m, side)


# ------------------------------------------------------------ 1. ladder arb


def ladder_arb(
    markets: Sequence[MarketRow], candles: dict[str, list[Candle]], margin: float = 0.005
) -> list[EdgeTrade]:
    by_event: dict[str, list[MarketRow]] = defaultdict(list)
    for m in markets:
        if m["strike_type"] == "greater" and m["floor"] is not None:
            by_event[m["event_ticker"]].append(m)
    out = []
    for ms in by_event.values():
        ms = sorted(ms, key=lambda m: m["floor"])
        quotes = {m["ticker"]: {c["end_ts"]: c for c in candles.get(m["ticker"], [])} for m in ms}
        times = sorted({t for q in quotes.values() for t in q})
        taken: set[tuple[str, str]] = set()
        for t in times:
            live: list[tuple[MarketRow, Candle]] = []
            for m in ms:
                c = quotes[m["ticker"]].get(t)
                if c is not None and 0 < c["bid"] < c["ask"] < 1:
                    live.append((m, c))
            for i, (m1, c1) in enumerate(live):
                for m2, c2 in live[i + 1 :]:
                    yes, no = c1["ask"], 1 - c2["bid"]
                    cost = yes + no + fee(yes) + fee(no)
                    key = (m1["ticker"], m2["ticker"])
                    if cost < 1 - margin and key not in taken:
                        taken.add(key)
                        payout = _settle(m1, "yes") + _settle(m2, "no")
                        out.append(EdgeTrade("ladder_arb", key[0], t, "box", cost, payout - cost))
    return out


# ------------------------------------------------------------ 2. early leader


def early_leader(markets: Sequence[MarketRow], candles: dict[str, list[Candle]]) -> list[EdgeTrade]:
    out = []
    for m in markets:
        cs = candles.get(m["ticker"], [])
        peak = {"yes": 0.0, "no": 0.0}
        for i, c in enumerate(cs):
            minute = (c["end_ts"] - m["open_ts"]) // 60
            for side in ("yes", "no"):
                ask, _ = _side_px(c, side)
                if 2 <= minute <= 3 and 0.56 <= ask <= 0.60 and ask >= peak[side] - 0.10:
                    proceeds = _exit_at_target(cs, i + 1, side, 0.99, m)
                    out.append(
                        EdgeTrade(
                            "early_leader",
                            m["ticker"],
                            c["end_ts"],
                            side,
                            ask,
                            proceeds - ask - fee(ask),
                        )
                    )
                    break
                peak[side] = max(peak[side], ask)
            else:
                continue
            break
    return out


# ------------------------------------------------------------ 3. reclaim


def _ema(values: Sequence[float], span: int) -> float:
    a = 2 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = a * v + (1 - a) * e
    return e


def reclaim(
    markets: Sequence[MarketRow], candles: dict[str, list[Candle]], spot: Spot
) -> list[EdgeTrade]:
    out = []
    for m in markets:
        cs = candles.get(m["ticker"], [])
        by_min = {(c["end_ts"] - m["open_ts"]) // 60: c for c in cs}
        early = [by_min[k] for k in range(1, 6) if k in by_min]
        m5 = by_min.get(5)
        s_open, s5 = spot.at(m["open_ts"]), spot.at(m["open_ts"] + 300)
        if not early or m5 is None or s_open is None or s5 is None:
            continue
        closes15 = [spot.at(m["open_ts"] - 900 * k) for k in range(21, 0, -1)]
        if any(x is None for x in closes15):
            continue
        ema = _ema([float(x) for x in closes15 if x is not None], 21)
        yes_low = min(c["ask"] for c in early)
        no_low = min(1 - c["bid"] for c in early)
        btc_move = s5 - s_open
        for k in range(6, 11):
            c, prev = by_min.get(k), by_min.get(k - 1)
            if c is None or prev is None:
                continue
            i = cs.index(c)
            yes_ask, no_ask = c["ask"], 1 - c["bid"]
            yes_lane = (
                0.15 < yes_low <= 0.30
                and 0.35 <= yes_ask < 0.55
                and yes_ask > m5["ask"]
                and yes_ask > prev["ask"]  # "green" minute on the YES quote
                and abs(btc_move) <= 20
                and 50 <= ema - s5 <= 150
            )
            no_lane = 0.35 < no_low <= 0.40 and 0.50 <= no_ask < 0.70 and 25 <= btc_move <= 75
            if yes_lane or no_lane:
                side, ask, target = ("yes", yes_ask, 0.70) if yes_lane else ("no", no_ask, 0.80)
                proceeds = _exit_at_target(cs, i + 1, side, target, m)
                out.append(
                    EdgeTrade(
                        "reclaim", m["ticker"], c["end_ts"], side, ask, proceeds - ask - fee(ask)
                    )
                )
                break
    return out


# ------------------------------------------------------------ 4. expiry decay


def expiry_decay(
    markets: Sequence[MarketRow], candles: dict[str, list[Candle]], spot: Spot
) -> list[EdgeTrade]:
    out = []
    for m in markets:
        strike = m["floor"] if m["floor"] is not None else m["cap"]
        if strike is None or m["strike_type"] not in ("greater", "greater_or_equal"):
            continue
        for c in candles.get(m["ticker"], []):
            left = m["close_ts"] - c["end_ts"]
            if not 120 <= left <= 180:
                continue
            t = c["end_ts"]
            s = spot.at(t)
            closes = [spot.at(t - 60 * k) for k in range(15, -1, -1)]
            if s is None or any(x is None for x in closes):
                continue
            r = np.diff(np.log(np.array(closes, dtype=np.float64)))
            sigma_s = float(r.std(ddof=1)) / math.sqrt(60)
            if sigma_s <= 0:
                continue
            # 60s averaging: variance over the pre-window part + 1/3 of the window
            sd = sigma_s * math.sqrt((left - 60) + 20)
            z = math.log(s / strike) / sd
            p_yes = 0.5 * math.erfc(-z / math.sqrt(2))
            if abs(z) < 2:
                continue
            side = "yes" if z > 0 else "no"
            p = p_yes if side == "yes" else 1 - p_yes
            ask, _ = _side_px(c, side)
            if 0.40 <= ask <= 0.75 and p - ask >= 0.03:
                out.append(
                    EdgeTrade(
                        "expiry_decay", m["ticker"], t, side, ask, _settle(m, side) - ask - fee(ask)
                    )
                )
                break
    return out


# ------------------------------------------------------------ runner


def evaluate(
    name: str,
    trades: Sequence[EdgeTrade],
    holdout_start: int,
    note: str = "",
) -> EdgeResult:
    tr = np.array([x.pnl for x in trades if x.ts < holdout_start], dtype=np.float64)
    ho = np.array([x.pnl for x in trades if x.ts >= holdout_start], dtype=np.float64)
    return EdgeResult(name, mean_ci(tr), mean_ci(ho), note)


def run_all(
    data: dict[str, tuple[Sequence[MarketRow], dict[str, list[Candle]]]],
    spot: Spot,
    holdout_start: int,
) -> list[EdgeResult]:
    hourly = data.get("KXBTCD")
    m15 = data.get("KXBTC15M")
    jobs: list[tuple[str, Callable[[], list[EdgeTrade]], str]] = []
    if hourly:
        jobs += [
            (
                "ladder_arb (KXBTCD)",
                lambda: ladder_arb(*hourly),
                "upper bound: minute-close quotes",
            ),
            ("expiry_decay (KXBTCD)", lambda: expiry_decay(*hourly, spot), ""),
        ]
    if m15:
        jobs += [
            ("early_leader (KXBTC15M)", lambda: early_leader(*m15), ""),
            (
                "reclaim (KXBTC15M)",
                lambda: reclaim(*m15, spot),
                "published thresholds, likely overfit",
            ),
            ("expiry_decay (KXBTC15M)", lambda: expiry_decay(*m15, spot), ""),
        ]
    return [evaluate(name, fn(), holdout_start, note) for name, fn, note in jobs]


def render(results: Sequence[EdgeResult]) -> str:
    lines = [
        "edge                        | train n  mean $/ct [95% CI]"
        "        | holdout n  mean $/ct [95% CI]"
    ]
    for r in results:
        tr, ho = r.train, r.holdout
        lines.append(
            f"{r.edge:<27} | {tr.n:>7} {tr.mean:+.4f} [{tr.ci_lo:+.4f},{tr.ci_hi:+.4f}]"
            f" | {ho.n:>9} {ho.mean:+.4f} [{ho.ci_lo:+.4f},{ho.ci_hi:+.4f}]"
            + (f"  ({r.note})" if r.note else "")
        )
    return "\n".join(lines)
