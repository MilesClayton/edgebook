"""One pass over recorded data testing three structural edges.

A. Parity between the 15-minute and hourly markets. Both settle on the same
   number (the 60-second BRTI average before the close), so at a shared close:
   15-min UP + hourly "above K" NO pays at least $1 for any K >= the 15-min
   start level S, and 15-min DOWN + hourly "above K" YES pays at least $1 for
   any K < S. A pair that costs less than $1 after taker fees is an arbitrage.
   Only the nearest strikes matter (they are the cheapest hedges).

B. The final minute. Inside the last 60 s part of the settlement average is
   already printed, so the outcome firms up fast. Every index tick in the last
   90 s, the model's fair value (with the known partial average) is compared
   with the ask; the first qualifying entry per market is scored on the result.

C. Lag after index jumps. When the index moves sharply, does Kalshi reprice
   slowly enough to buy at the stale ask and sell into the new bid? Markouts
   (bid on the side bought, h seconds later, minus the ask paid, minus both
   fees) are measured at 1, 2, 5, 10 and 30 s, plus hold-to-settlement.

Fills use top-of-book prices and sizes only (no queue, no latency) so every
number here is an upper bound on what a real taker could get.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.engine.decide import EngineConfig, fair_up
from edgebook.engine.state import INDEX_KEEP_S, LiveState, MarketInfo, _index_value
from edgebook.model.calibration import MeanCI, mean_ci
from edgebook.research.engine_replay import ReplayMarket, _apply_book, events, load_markets

MARKOUT_S = (1, 2, 5, 10, 30)
WINDOW_S = 90  # final-minute scan starts this long before the close
PARITY_EVERY_MS = 250
JUMP_COOLDOWN_MS = 10_000  # one event per market per jump, not one per tick


def taker_fee(price: float, contracts: int = 100) -> float:
    """Per-contract taker fee for a ``contracts``-lot (Kalshi rounds the total up to a cent)."""
    p = min(max(price, 0.0), 1.0)
    return float(trade_fee(contracts, Decimal(f"{p:.4f}"), LiquidityRole.TAKER)) / contracts


def _best_ask(state: LiveState, ticker: str, side: str) -> tuple[float, float] | None:
    lv = state.asks(ticker, side)
    return (lv[0].price, lv[0].size) if lv else None


def _best_bid(state: LiveState, ticker: str, side: str) -> tuple[float, float] | None:
    lv = state.bids(ticker, side)
    return (lv[0].price, lv[0].size) if lv else None


# ---------------------------------------------------------------- A. parity


@dataclass
class ParityStats:
    close_ts: int
    fifteen: str
    checks: int = 0
    min_cost: float = math.inf  # incl. fees, $ for a pair that pays >= $1
    min_cost_pair: str = ""
    below_one_ms: int = 0  # time some pair cost < $1 after fees
    best_size_below_one: float = 0.0  # contracts available on both legs at the best moment
    first_below_ms: int | None = None


def _parity_pairs(
    state: LiveState, fifteen: MarketInfo, hourly: list[MarketInfo]
) -> list[tuple[str, float, float]]:
    """(label, cost incl. fees, size on both legs) for the two nearest hedges."""
    s = fifteen.floor
    if s is None:
        return []
    above = sorted(
        (m for m in hourly if m.floor is not None and m.floor >= s), key=lambda m: m.floor or 0
    )
    below = sorted(
        (m for m in hourly if m.floor is not None and m.floor < s), key=lambda m: -(m.floor or 0)
    )
    out = []
    up = _best_ask(state, fifteen.ticker, "yes")
    down = _best_ask(state, fifteen.ticker, "no")
    for m in above[:2]:
        no = _best_ask(state, m.ticker, "no")
        if up and no:
            cost = up[0] + no[0] + taker_fee(up[0]) + taker_fee(no[0])
            out.append((f"15m UP + {m.ticker} NO", cost, min(up[1], no[1])))
    for m in below[:2]:
        yes = _best_ask(state, m.ticker, "yes")
        if down and yes:
            cost = down[0] + yes[0] + taker_fee(down[0]) + taker_fee(yes[0])
            out.append((f"15m DOWN + {m.ticker} YES", cost, min(down[1], yes[1])))
    return out


# ---------------------------------------------------------------- B. final minute


@dataclass
class WindowSample:
    ticker: str
    series: str
    secs_left: float
    fair_up: float
    up_ask: float | None
    down_ask: float | None


# ---------------------------------------------------------------- C. markouts


@dataclass
class JumpEvent:
    ts_ms: int
    ticker: str
    series: str
    side: str  # side bought: "yes" if the index jumped up (relative to "above" markets)
    move_bps: float
    entry: float  # ask paid
    entry_fee: float
    marks: dict[int, float] = field(default_factory=dict)  # h -> bid on that side after h s


@dataclass
class ScanResult:
    parity: list[ParityStats]
    window: list[WindowSample]
    jumps: list[JumpEvent]
    results: dict[str, str]
    markets: dict[str, ReplayMarket]


def scan(
    directory: Path,
    start_ms: int,
    end_ms: int,
    jump_bps: float = 3.0,
    jump_lookback_s: float = 2.0,
    cfg: EngineConfig | None = None,
) -> ScanResult:
    cfg = cfg or EngineConfig()
    markets = load_markets(directory)
    state = LiveState()
    state.markets = {tk: m.info for tk, m in markets.items()}
    by_close: dict[int, list[MarketInfo]] = defaultdict(list)
    for m in state.markets.values():
        by_close[m.close_ts].append(m)
    shared = {
        c: ms
        for c, ms in by_close.items()
        if any(m.series == "KXBTC15M" for m in ms) and any(m.series == "KXBTCD" for m in ms)
    }
    parity: dict[int, ParityStats] = {}
    for c, ms in shared.items():
        f = next(m for m in ms if m.series == "KXBTC15M")
        parity[c] = ParityStats(c, f.ticker)
    closes = sorted(by_close)
    last_jump_ms: dict[str, int] = {}
    last_parity_ms: dict[int, int] = {}
    below_since: dict[int, int | None] = {}
    window: list[WindowSample] = []
    jumps: list[JumpEvent] = []
    open_jumps: list[JumpEvent] = []

    def nearest_watch(now_s: float) -> list[MarketInfo]:
        """The open 15-min market and the hourly strikes nearest the index (next close)."""
        cur = state.index_now()
        out: list[MarketInfo] = []
        hourly: list[MarketInfo] = []
        for c in closes[bisect.bisect_right(closes, now_s) :]:
            if c > now_s + 3600:
                break
            for m in by_close[c]:
                if m.open_ts > now_s:
                    continue
                if m.series == "KXBTC15M":
                    out.append(m)
                elif m.series == "KXBTCD" and m.floor is not None and not hourly:
                    hourly = [
                        x for x in by_close[c] if x.series == "KXBTCD" and x.floor is not None
                    ]
        if hourly and cur:
            out += sorted(hourly, key=lambda m: abs((m.floor or 0) - cur[1]))[:3]
        return out

    def index_at_or_before(t_ms: float) -> float | None:
        for ts, v in reversed(state.index):
            if ts <= t_ms:
                return v
        return None

    for recv_ms, _, stream, r in events(directory, start_ms, end_ms):
        if stream == "book":
            state.ws_last_ms = recv_ms
            _apply_book(state, r)
            tk = r["ticker"]
            bm = state.markets.get(tk)
            if bm is None or bm.close_ts not in parity:
                continue
            c = bm.close_ts
            if recv_ms - last_parity_ms.get(c, 0) < PARITY_EVERY_MS or recv_ms >= c * 1000:
                continue
            last_parity_ms[c] = recv_ms
            st = parity[c]
            fifteen = state.markets[st.fifteen]
            hourly = [x for x in shared[c] if x.series == "KXBTCD"]
            pairs = _parity_pairs(state, fifteen, hourly)
            if not pairs:
                continue
            st.checks += 1
            label, cost, size = min(pairs, key=lambda p: p[1])
            if cost < st.min_cost:
                st.min_cost, st.min_cost_pair = cost, label
            since = below_since.get(c)
            if cost < 1.0:
                if since is None:
                    below_since[c] = recv_ms
                    st.first_below_ms = st.first_below_ms or recv_ms
                st.best_size_below_one = max(st.best_size_below_one, size)
            elif since is not None:
                st.below_one_ms += recv_ms - since
                below_since[c] = None
        elif stream == "trades":
            state.ws_last_ms = recv_ms
        elif r["index_id"] == state.index_id:
            value = _index_value(r["raw"])
            if value is None:
                continue
            state.ws_last_ms = recv_ms
            state.index.append((recv_ms, value))
            while state.index and state.index[0][0] < recv_ms - INDEX_KEEP_S * 1000:
                state.index.popleft()
            now_s = recv_ms / 1000
            # C: fill markouts of earlier jumps
            still = []
            for j in open_jumps:
                age = (recv_ms - j.ts_ms) / 1000
                for h in MARKOUT_S:
                    if h not in j.marks and age >= h:
                        bid = _best_bid(state, j.ticker, j.side)
                        if bid is not None:
                            j.marks[h] = bid[0]
                if len(j.marks) < len(MARKOUT_S) and age < max(MARKOUT_S) + 5:
                    still.append(j)
            open_jumps = still
            if state.history_s(recv_ms) < cfg.min_history_s:
                continue
            watch = nearest_watch(now_s)
            # C: detect a jump
            past = index_at_or_before(recv_ms - jump_lookback_s * 1000)
            if past:
                move = (value / past - 1) * 1e4
                if abs(move) >= jump_bps:
                    for m in watch:
                        if m.close_ts - now_s < 60:
                            continue
                        if (
                            recv_ms - last_jump_ms.get(m.ticker, -JUMP_COOLDOWN_MS)
                            < JUMP_COOLDOWN_MS
                        ):
                            continue
                        last_jump_ms[m.ticker] = recv_ms
                        up_market = m.strike_type not in ("less", "less_or_equal")
                        side = "yes" if (move > 0) == up_market else "no"
                        ask = _best_ask(state, m.ticker, side)
                        if ask is None:
                            continue
                        j = JumpEvent(
                            recv_ms, m.ticker, m.series, side, move, ask[0], taker_fee(ask[0])
                        )
                        jumps.append(j)
                        open_jumps.append(j)
            # B: final-minute samples
            for m in watch:
                left = m.close_ts - now_s
                if not 0 < left <= WINDOW_S:
                    continue
                p = fair_up(state, m, recv_ms, cfg)
                if p is None:
                    continue
                up = _best_ask(state, m.ticker, "yes")
                dn = _best_ask(state, m.ticker, "no")
                window.append(
                    WindowSample(
                        m.ticker, m.series, left, p, up[0] if up else None, dn[0] if dn else None
                    )
                )
    for c, since in below_since.items():
        if since is not None:
            parity[c].below_one_ms += c * 1000 - since
    results = {tk: m.result for tk, m in markets.items() if m.result in ("yes", "no")}
    return ScanResult(
        sorted(parity.values(), key=lambda s: s.close_ts), window, jumps, results, markets
    )


# ---------------------------------------------------------------- reports


def window_report(
    res: ScanResult, thresholds: tuple[float, ...] = (0.02, 0.04, 0.08), max_left: float = 60.0
) -> dict[str, dict[str, Any]]:
    """First qualifying entry per market inside ``max_left`` seconds, scored on the result."""
    out: dict[str, dict[str, Any]] = {}
    for th in thresholds:
        taken: dict[str, float] = {}
        for w in sorted(res.window, key=lambda w: -w.secs_left):
            if w.ticker in taken or w.secs_left > max_left or w.ticker not in res.results:
                continue
            won_up = res.results[w.ticker] == "yes"
            for side, fair, ask in (
                ("yes", w.fair_up, w.up_ask),
                ("no", 1 - w.fair_up, w.down_ask),
            ):
                if ask is None or not 0 < ask < 1:
                    continue
                if fair - ask - taker_fee(ask) >= th:
                    win = won_up if side == "yes" else not won_up
                    taken[w.ticker] = (1.0 if win else 0.0) - ask - taker_fee(ask)
                    break
        ci = mean_ci(np.array(list(taken.values()), dtype=float))
        out[f"edge>={th:.2f}"] = {"n": ci.n, "mean": ci.mean, "ci_lo": ci.ci_lo, "ci_hi": ci.ci_hi}
    return out


def markout_report(res: ScanResult) -> dict[str, dict[str, Any]]:
    """Round trip per contract: bid after h s minus ask paid minus both fees; and hold."""
    out: dict[str, dict[str, Any]] = {}
    for series in ("KXBTC15M", "KXBTCD"):
        js = [j for j in res.jumps if j.series == series]
        row: dict[str, Any] = {"jumps": len(js)}
        for h in MARKOUT_S:
            vals = [
                j.marks[h] - taker_fee(j.marks[h]) - j.entry - j.entry_fee
                for j in js
                if h in j.marks
            ]
            row[f"{h}s"] = _ci(mean_ci(np.array(vals, dtype=float)))
        hold = []
        for j in js:
            r = res.results.get(j.ticker)
            if r in ("yes", "no"):
                hold.append((1.0 if r == j.side else 0.0) - j.entry - j.entry_fee)
        row["hold"] = _ci(mean_ci(np.array(hold, dtype=float)))
        out[series] = row
    return out


def parity_report(res: ScanResult) -> dict[str, Any]:
    stats = [s for s in res.parity if s.checks]
    below = [s for s in stats if s.below_one_ms > 0]
    return {
        "closes_checked": len(stats),
        "closes_with_arb": len(below),
        "min_cost": min((s.min_cost for s in stats), default=None),
        "median_min_cost": float(np.median([s.min_cost for s in stats])) if stats else None,
        "arbs": [
            {
                "close_ts": s.close_ts,
                "min_cost": s.min_cost,
                "pair": s.min_cost_pair,
                "seconds_below_1": s.below_one_ms / 1000,
                "size": s.best_size_below_one,
            }
            for s in below
        ],
    }


def _ci(ci: MeanCI) -> dict[str, Any]:
    return {"n": ci.n, "mean": ci.mean, "ci_lo": ci.ci_lo, "ci_hi": ci.ci_hi}
