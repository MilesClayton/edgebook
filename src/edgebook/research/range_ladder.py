"""Are the hourly BTC range markets priced consistently with the above/below ladder?

Kalshi lists, for the same hourly close, a *range* series (KXBTC: "BTC ends between
A and A+99.99", plus two open-ended tails) and a *ladder* (KXBTCD: "BTC ends above K"
at K = ...99.99). Both settle on the same number quoted to the cent, so

    range [A, A+99.99]  pays  above(A - 0.01) - above(A + 99.99)
    tail  "above F"     pays  above(F)                (the identical contract)
    tail  "below C"     pays  1 - above(C - 0.01)

Kalshi only lets you *buy* YES or NO, so each inconsistency is a basket of purchases
whose payoff is the same in every outcome:

- ``buy_range``: range YES + lower NO + upper YES pays exactly $1;
- ``sell_range``: range NO + lower YES + upper NO pays exactly $2 ($1 for a tail,
  which has only one ladder leg; see ``basket_payoff``).

If a basket costs less than its payoff after the taker fee on every leg, with size on
every leg at the same moment, the prices contradict each other.

The book stream is replayed once per ticker into a top-of-book change series
(``top_of_book``). Every range is then aligned with its two ladder legs on the union of
their change times (vectorised), so "the same moment" means the latest recorded state of
all three books. Recorder outages (no book message for ``OUTAGE_MS``) invalidate every
book until its next snapshot, so a frozen book never shows up as a long-lived gap.

Fees are Kalshi's general taker schedule (``core.fees``), rounded up to the cent per
leg on the lot that top-of-book depth allows. Fills ignore queue and latency, so every
gap here is an upper bound on what a taker could capture.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from edgebook.engine.maker import LongshotConfig
from edgebook.engine.state import MarketInfo
from edgebook.research.edge_scan import taker_fee
from edgebook.research.longshot_gaps import event_ci

P = 10_000  # price units: 1/10000 of a dollar (the recorder stores 4 dp)
OUTAGE_MS = 5_000  # no book message for this long = the recorder was down
TAKER_COEF_X100 = 7  # 0.07, as an integer for exact cent rounding
RANGE = "KXBTC"
LADDER = "KXBTCD"
DIRECTIONS = ("buy_range", "sell_range")
LONGSHOT = LongshotConfig()  # the paper long-shot seller's YES ask band and time window

I64 = np.ndarray[Any, np.dtype[np.int64]]
F64 = np.ndarray[Any, np.dtype[np.float64]]


# ---------------------------------------------------------------- matching


@dataclass(frozen=True)
class RangeLegs:
    """A KXBTC market and the KXBTCD strikes that replicate it.

    The range pays ``YES(lower) - YES(upper)``; a missing ``lower`` stands for a
    contract that always pays (the "below" tail), a missing ``upper`` for one that
    never pays (the "above" tail).
    """

    ticker: str
    close_ts: int
    strike_type: str
    lower: str | None
    upper: str | None


def _cents(x: float) -> int:
    return round(x * 100)


def match_ranges(markets: Mapping[str, MarketInfo]) -> tuple[list[RangeLegs], list[str]]:
    """Match every KXBTC market to its ladder legs at the same close.

    Returns (matched, unmatched range tickers). Strikes are compared in whole cents, so
    a range floor of 81000.00 matches the ladder strike 80999.99 exactly.
    """
    ladder: dict[tuple[int, int], str] = {}
    for m in markets.values():
        if m.series == LADDER and m.strike_type == "greater" and m.floor is not None:
            ladder[(m.close_ts, _cents(m.floor))] = m.ticker
    matched: list[RangeLegs] = []
    unmatched: list[str] = []
    for m in sorted(markets.values(), key=lambda m: m.ticker):
        if m.series != RANGE:
            continue
        lower: str | None = None
        upper: str | None = None
        ok = False
        if m.strike_type == "between" and m.floor is not None and m.cap is not None:
            lower = ladder.get((m.close_ts, _cents(m.floor) - 1))
            upper = ladder.get((m.close_ts, _cents(m.cap)))
            ok = lower is not None and upper is not None
        elif m.strike_type == "greater" and m.floor is not None:
            lower = ladder.get((m.close_ts, _cents(m.floor)))
            ok = lower is not None
        elif m.strike_type == "less" and m.cap is not None:
            upper = ladder.get((m.close_ts, _cents(m.cap) - 1))
            ok = upper is not None
        if ok:
            matched.append(RangeLegs(m.ticker, m.close_ts, m.strike_type, lower, upper))
        else:
            unmatched.append(m.ticker)
    return matched, unmatched


def replicated_result(legs: RangeLegs, results: Mapping[str, str]) -> str | None:
    """The range's result implied by its ladder legs' results (None if one is missing)."""
    lo = True if legs.lower is None else results.get(legs.lower)
    hi = False if legs.upper is None else results.get(legs.upper)
    if lo is None or hi is None:
        return None
    lo_yes = lo is True or lo == "yes"
    hi_yes = hi is True or hi == "yes"
    return "yes" if lo_yes and not hi_yes else "no"


def check_results(legs: Sequence[RangeLegs], results: Mapping[str, str]) -> dict[str, int]:
    """Confirm the boundary convention on settled markets: replicated == official."""
    n = bad = 0
    for r in legs:
        rep = replicated_result(r, results)
        if rep is None or results.get(r.ticker) not in ("yes", "no"):
            continue
        n += 1
        bad += rep != results[r.ticker]
    return {"settled_checked": n, "mismatches": bad}


Basket = tuple[tuple[str, str], ...]  # (ticker, side bought)


def baskets(r: RangeLegs) -> dict[str, Basket]:
    buy: list[tuple[str, str]] = [(r.ticker, "yes")]
    sell: list[tuple[str, str]] = [(r.ticker, "no")]
    if r.lower is not None:
        buy.append((r.lower, "no"))
        sell.append((r.lower, "yes"))
    if r.upper is not None:
        buy.append((r.upper, "yes"))
        sell.append((r.upper, "no"))
    return {"buy_range": tuple(buy), "sell_range": tuple(sell)}


def basket_payoff(r: RangeLegs, direction: str) -> int:
    """Dollars a basket pays in every outcome."""
    if direction == "buy_range":
        return 1
    return int(r.lower is not None) + int(r.upper is not None)


# ---------------------------------------------------------------- fees


def fee_per_contract(price_units: I64, lot: I64) -> F64:
    """Kalshi taker fee per contract: ceil(0.07 * C * p * (1 - p)) to the cent, / C.

    Integer arithmetic so the cent rounding is exact; equals ``core.fees.trade_fee``.
    """
    p = price_units.astype(np.int64)
    c = np.maximum(lot.astype(np.int64), 1)
    num = TAKER_COEF_X100 * c * p * (P - p)  # cents * P**2
    cents = -(-num // (P * P))
    out: F64 = cents / 100.0 / c
    return out


# ---------------------------------------------------------------- books


@dataclass
class Tops:
    """Top-of-book after every change for one ticker (prices in ``P`` units, -1 = none)."""

    ts: I64
    yes_bid: I64
    yes_size: F64
    no_bid: I64
    no_size: F64

    def at(self, t: I64) -> tuple[I64, I64, F64, I64, F64]:
        """State in force at each time in ``t``: (time it was set, yes bid, yes size,
        no bid, no size); set time -1 and no bids before the first record."""
        idx = np.searchsorted(self.ts, t, side="right") - 1
        valid = idx >= 0
        j = np.where(valid, idx, 0)
        set_at = np.where(valid, self.ts[j], -1)
        yb = np.where(valid, self.yes_bid[j], -1)
        nb = np.where(valid, self.no_bid[j], -1)
        return set_at, yb, self.yes_size[j], nb, self.no_size[j]


class _Book:
    __slots__ = ("best", "last", "levels", "outage", "ready")

    def __init__(self) -> None:
        self.levels: tuple[dict[int, int], dict[int, int]] = ({}, {})
        self.best = [-1, -1]
        self.ready = False
        self.outage = 0  # outages seen before this book's last row
        self.last: tuple[int, int, int, int] | None = None

    def clear(self, ready: bool) -> None:
        self.levels = ({}, {})
        self.best = [-1, -1]
        self.ready = ready

    def set_level(self, s: int, price: int, qty: int) -> None:
        lv = self.levels[s]
        if qty <= 0:
            lv.pop(price, None)
            if price == self.best[s]:
                self.best[s] = max(lv) if lv else -1
        else:
            lv[price] = qty
            if price > self.best[s]:
                self.best[s] = price

    def delta(self, s: int, price: int, qty: int) -> None:
        size = self.levels[s].get(price, 0) + qty
        if size < 0:
            self.clear(False)  # inconsistent: unusable until the next snapshot
            return
        self.set_level(s, price, size)

    def top(self) -> tuple[int, int, int, int]:
        if not self.ready:
            return (-1, 0, -1, 0)
        y, n = self.best
        return (
            y,
            self.levels[0].get(y, 0),
            n,
            self.levels[1].get(n, 0),
        )


def book_files(directory: Path) -> list[Path]:
    return sorted(f for f in (directory / "book").rglob("*.parquet") if f.stat().st_size > 0)


def _read(f: Path, columns: list[str]) -> pa.Table | None:
    try:
        return pq.read_table(f, columns=columns)
    except (pa.ArrowInvalid, OSError):
        return None  # an empty/torn part file left by a restart mid-write


def outages(files: Iterable[Path], gap_ms: int = OUTAGE_MS) -> list[tuple[int, int]]:
    """(last message before, first message after) for every silence of ``gap_ms``+."""
    out: list[tuple[int, int]] = []
    prev: int | None = None
    for f in files:
        tbl = _read(f, ["recv_ts_ms"])
        if tbl is None or tbl.num_rows == 0:
            continue
        ts = np.sort(tbl["recv_ts_ms"].to_numpy())
        if prev is not None and ts[0] - prev >= gap_ms:
            out.append((prev, int(ts[0])))
        d = np.flatnonzero(np.diff(ts) >= gap_ms)
        out += [(int(ts[i]), int(ts[i + 1])) for i in d]
        prev = int(ts[-1]) if prev is None else max(prev, int(ts[-1]))
    return sorted(out)


def _rows(
    files: Iterable[Path], keep: set[str]
) -> Iterator[tuple[list[str], I64, list[str], I64, I64, I64]]:
    """Per file: (tickers, ts, kinds, side 0/1/-1, price units, qty in 1/100) sorted by
    ticker then arrival (stable, so a snapshot's levels stay after its reset)."""
    keep_arr = pa.array(sorted(keep))
    for f in files:
        tbl = _read(f, ["recv_ts_ms", "kind", "ticker", "side", "price", "qty"])
        if tbl is None:
            continue
        tbl = tbl.filter(pc.is_in(tbl["ticker"], value_set=keep_arr))
        if tbl.num_rows == 0:
            continue
        tickers = tbl["ticker"].to_pylist()
        ts = tbl["recv_ts_ms"].to_numpy()
        enc = pc.dictionary_encode(tbl["ticker"]).combine_chunks()
        codes = enc.indices.to_numpy()  # type: ignore[attr-defined]
        order = np.lexsort((np.arange(len(ts)), ts, codes))
        kinds = tbl["kind"].to_pylist()
        side = np.array(
            [0 if s == "yes" else 1 if s == "no" else -1 for s in tbl["side"].to_pylist()]
        )
        price = pc.cast(tbl["price"], pa.float64()).to_numpy(zero_copy_only=False)
        qty = pc.cast(tbl["qty"], pa.float64()).to_numpy(zero_copy_only=False)
        price_u = np.rint(np.nan_to_num(price) * P).astype(np.int64)
        qty_c = np.rint(np.nan_to_num(qty) * 100).astype(np.int64)
        yield (
            [tickers[i] for i in order],
            ts[order],
            [kinds[i] for i in order],
            side[order],
            price_u[order],
            qty_c[order],
        )


def top_of_book(
    files: Sequence[Path], keep: set[str], gaps: Sequence[tuple[int, int]] = ()
) -> dict[str, Tops]:
    """Replay book snapshots/deltas into a top-of-book change series per ticker."""
    starts = [g[0] for g in gaps]
    books: dict[str, _Book] = {}
    out: dict[str, list[tuple[int, int, int, int, int]]] = defaultdict(list)
    for tickers, ts, kinds, side, price, qty in _rows(files, keep):
        n_out = np.searchsorted(np.asarray(starts, dtype=np.int64), ts, side="left")
        tsl, sl, pl, ql, nl = ts.tolist(), side.tolist(), price.tolist(), qty.tolist(), n_out
        for i, tk in enumerate(tickers):
            b = books.get(tk)
            if b is None:
                b = books[tk] = _Book()
            k = int(nl[i])
            if k != b.outage:  # the recorder was down since this book's last message
                b.outage = k
                b.clear(False)
            kind = kinds[i]
            if kind == "reset":
                b.clear(True)
            elif b.ready:
                if kind == "level":
                    b.set_level(sl[i], pl[i], ql[i])
                else:
                    b.delta(sl[i], pl[i], ql[i])
            top = b.top()
            if top != b.last:
                b.last = top
                out[tk].append((tsl[i], *top))
    tops: dict[str, Tops] = {}
    for tk, rows in out.items():
        a = np.array(rows, dtype=np.int64)
        # one row per timestamp (a snapshot's reset and levels arrive together), then drop
        # rows that repeat the previous state
        a = a[np.append(a[1:, 0] != a[:-1, 0], True)]
        a = a[np.insert(np.any(a[1:, 1:] != a[:-1, 1:], axis=1), 0, True)]
        tops[tk] = Tops(
            a[:, 0], a[:, 1], a[:, 2].astype(np.float64) / 100, a[:, 3], a[:, 4] / 100.0
        )
    return tops


# ---------------------------------------------------------------- gap scan


@dataclass
class Episode:
    """A continuous stretch where one basket cost less than it pays, after fees."""

    range_ticker: str
    close_ts: int
    direction: str
    start_ms: int
    duration_ms: int
    secs_to_close: float
    max_edge: float  # $ per contract after fees, at the best moment
    lot: int  # contracts available on every leg at that moment
    dollars: float  # max over the episode of edge * lot
    prices: list[float] = field(default_factory=list)  # leg asks at the best moment


@dataclass
class RangeScan:
    range_ticker: str
    close_ts: int
    quoted_ms: int = 0  # time with a two-sided quote on every leg
    prefee_ms: dict[str, int] = field(default_factory=dict)  # time costing < payoff pre-fee
    best_edge: dict[str, float] = field(default_factory=dict)  # max edge after fees
    best_prefee: dict[str, float] = field(default_factory=dict)
    episodes: list[Episode] = field(default_factory=list)


def _outages_before(t: I64, starts: I64) -> I64:
    """Number of outages that began strictly before each time."""
    out: I64 = np.searchsorted(starts, t, side="left").astype(np.int64)
    return out


def scan_range(
    r: RangeLegs,
    tops: Mapping[str, Tops],
    end_ms: int,
    gaps: Sequence[tuple[int, int]] = (),
) -> RangeScan | None:
    """Every moment both baskets of one range were executable, and the gaps among them."""
    legs = [r.ticker] + [x for x in (r.lower, r.upper) if x is not None]
    if any(tk not in tops for tk in legs):
        return None
    stop = min(r.close_ts * 1000, end_ms)
    starts = np.array(sorted(a for a, _ in gaps), dtype=np.int64)
    # break the timeline 1 ms after each outage starts, so a book's last pre-outage state
    # lasts 1 ms rather than until its next post-outage message
    t = np.unique(np.concatenate([tops[tk].ts for tk in legs] + [starts + 1]))
    t = t[t < stop]
    if len(t) == 0:
        return None
    dt = np.diff(np.append(t, stop))
    state = {tk: tops[tk].at(t) for tk in legs}
    k_now = _outages_before(t, starts)
    good = np.ones(len(t), dtype=bool)
    for set_at, yb, _, nb, _ in state.values():
        good &= set_at >= 0
        good &= _outages_before(set_at, starts) == k_now  # not left over from before an outage
        good &= (yb >= 0) & (nb >= 0) & (yb + nb < P)  # two-sided and not crossed
    out = RangeScan(r.ticker, r.close_ts, quoted_ms=int(dt[good].sum()))
    for direction, basket in baskets(r).items():
        cost = np.zeros(len(t), dtype=np.int64)
        lot_f = np.full(len(t), np.inf)
        asks = []
        for tk, side in basket:
            _, yb, ys, nb, ns = state[tk]
            ask = P - nb if side == "yes" else P - yb  # buy YES from NO bids and vice versa
            size = ns if side == "yes" else ys
            cost += ask
            lot_f = np.minimum(lot_f, size)
            asks.append(ask)
        payoff = basket_payoff(r, direction) * P
        lot = np.floor(np.where(np.isfinite(lot_f), lot_f, 0)).astype(np.int64)
        ok = good & (lot >= 1)
        fees = np.zeros(len(t))
        for ask in asks:
            fees += fee_per_contract(ask, lot)
        prefee = (payoff - cost) / P
        edge = prefee - fees
        out.prefee_ms[direction] = int(dt[ok & (prefee > 0)].sum())
        out.best_prefee[direction] = float(prefee[ok].max()) if ok.any() else math.nan
        out.best_edge[direction] = float(edge[ok].max()) if ok.any() else math.nan
        hit = ok & (edge > 0)
        if not hit.any():
            continue
        # runs of consecutive hits
        idx = np.flatnonzero(hit)
        breaks = np.flatnonzero(np.diff(idx) > 1)
        for seg in np.split(idx, breaks + 1):
            best = seg[np.argmax(edge[seg])]
            dollars = edge[seg] * lot[seg]
            out.episodes.append(
                Episode(
                    range_ticker=r.ticker,
                    close_ts=r.close_ts,
                    direction=direction,
                    start_ms=int(t[seg[0]]),
                    duration_ms=int(dt[seg].sum()),
                    secs_to_close=r.close_ts - t[seg[0]] / 1000,
                    max_edge=float(edge[best]),
                    lot=int(lot[best]),
                    dollars=float(dollars.max()),
                    prices=[float(a[best]) / P for a in asks],
                )
            )
    return out


# ---------------------------------------------------------------- reports


def _boot_mean(x: Sequence[float], n_boot: int = 2000, seed: int = 0) -> dict[str, float]:
    """Mean per close with a 95% CI from resampling closes."""
    arr = np.asarray(x, dtype=float)
    if len(arr) == 0:
        return {"n": 0, "mean": math.nan, "ci_lo": math.nan, "ci_hi": math.nan}
    rng = np.random.default_rng(seed)
    means = arr[rng.integers(0, len(arr), size=(n_boot, len(arr)))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {"n": len(arr), "mean": float(arr.mean()), "ci_lo": float(lo), "ci_hi": float(hi)}


def _q(x: Sequence[float], qs: Sequence[float] = (0.5, 0.9, 0.99)) -> dict[str, float]:
    if len(x) == 0:
        return {}
    return {f"p{round(q * 100)}": float(np.quantile(np.asarray(x, dtype=float), q)) for q in qs}


def per_close(
    scans: Sequence[RangeScan], min_ms: int = 0, min_lot: int = 1
) -> list[dict[str, Any]]:
    """One row per close: quoted range-time, gap counts/time/dollars after fees."""
    by: dict[int, list[RangeScan]] = defaultdict(list)
    for s in scans:
        by[s.close_ts].append(s)
    rows = []
    for c in sorted(by):
        ss = by[c]
        eps = [e for s in ss for e in s.episodes if e.duration_ms >= min_ms and e.lot >= min_lot]
        quoted = sum(s.quoted_ms for s in ss)
        prefee = sum(sum(s.prefee_ms.values()) for s in ss)
        best = [v for s in ss for v in s.best_edge.values() if not math.isnan(v)]
        rows.append(
            {
                "close_ts": c,
                "ranges": len(ss),
                "quoted_range_hours": quoted / 3.6e6,
                "prefee_gap_share": prefee / quoted if quoted else 0.0,
                "episodes": len(eps),
                "gap_seconds": sum(e.duration_ms for e in eps) / 1000,
                "dollars": sum(e.dollars for e in eps),
                "best_edge": max(best) if best else math.nan,
            }
        )
    return rows


def summarize(scans: Sequence[RangeScan]) -> dict[str, Any]:
    eps = [e for s in scans for e in s.episodes]
    out: dict[str, Any] = {
        "ranges_scanned": len(scans),
        "closes": len({s.close_ts for s in scans}),
        "quoted_range_hours": sum(s.quoted_ms for s in scans) / 3.6e6,
        "episodes": len(eps),
        "by_direction": {},
    }
    for d in DIRECTIONS:
        de = [e for e in eps if e.direction == d]
        best = [s.best_edge[d] for s in scans if not math.isnan(s.best_edge.get(d, math.nan))]
        pre = [s.best_prefee[d] for s in scans if not math.isnan(s.best_prefee.get(d, math.nan))]
        out["by_direction"][d] = {
            "episodes": len(de),
            "duration_ms": _q([e.duration_ms for e in de]),
            "edge_cents": _q([e.max_edge * 100 for e in de]),
            "lot": _q([e.lot for e in de]),
            "dollars_total": sum(e.dollars for e in de),
            "range_best_edge_cents": _q([b * 100 for b in best], (0.5, 0.9, 0.99, 1.0)),
            "range_best_prefee_cents": _q([b * 100 for b in pre], (0.5, 0.9, 0.99, 1.0)),
        }
    for label, min_ms, min_lot in (
        ("all", 0, 1),
        ("lasting>=1s", 1000, 1),
        ("lasting>=1s,lot>=10", 1000, 10),
    ):
        rows = per_close(scans, min_ms, min_lot)
        out[f"per_close[{label}]"] = {
            "closes_with_gap": sum(r["episodes"] > 0 for r in rows),
            "episodes_per_close": _boot_mean([r["episodes"] for r in rows]),
            "gap_seconds_per_close": _boot_mean([r["gap_seconds"] for r in rows]),
            "dollars_per_close": _boot_mean([r["dollars"] for r in rows]),
        }
    rows = per_close(scans)
    out["prefee_gap_share_per_close"] = _boot_mean([r["prefee_gap_share"] for r in rows])
    out["best_edge_cents_per_close"] = _q(
        [r["best_edge"] * 100 for r in rows if not math.isnan(r["best_edge"])], (0.1, 0.5, 0.9, 1.0)
    )
    return out


# ---------------------------------------------------------------- longshot fills


@dataclass
class FillCheck:
    ticker: str
    close_ts: int
    ts_ms: int
    side: str  # side bought
    contracts: float
    price: float
    won: bool | None
    ladder_mid: float | None  # ladder-implied value of the side bought (mid of each leg)
    ladder_bid: float | None  # what the ladder would pay for that side right now
    ladder_ask: float | None  # what the side costs built from the ladder (no fees)
    range_mid: float | None  # the range's own mid at the fill


def _mid(tops: Tops | None, t: int) -> tuple[float | None, float | None, float | None]:
    """(yes bid, yes ask, mid) in dollars at time t (all None for an unrecorded book)."""
    if tops is None:
        return None, None, None
    _, yb, _, nb, _ = tops.at(np.array([t], dtype=np.int64))
    bid = float(yb[0]) / P if yb[0] >= 0 else None
    ask = float(P - nb[0]) / P if nb[0] >= 0 else None
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    return bid, ask, mid


def check_fills(
    fills: Sequence[Mapping[str, Any]],
    legs: Mapping[str, RangeLegs],
    tops: Mapping[str, Tops],
    results: Mapping[str, str],
) -> list[FillCheck]:
    """Compare each KXBTC paper buy with the ladder-implied price at that instant."""
    out = []
    for f in fills:
        r = legs.get(f["ticker"])
        if f["action"] != "BUY" or r is None:
            continue
        t = int(f["ts_ms"])
        lo = (1.0, 1.0, 1.0) if r.lower is None else _mid(tops.get(r.lower), t)
        hi = (0.0, 0.0, 0.0) if r.upper is None else _mid(tops.get(r.upper), t)
        yes_mid = lo[2] - hi[2] if lo[2] is not None and hi[2] is not None else None
        # build range YES = lower YES - upper YES: buy at lower ask - upper bid, sell at
        # lower bid - upper ask
        yes_ask = lo[1] - hi[0] if lo[1] is not None and hi[0] is not None else None
        yes_bid = lo[0] - hi[1] if lo[0] is not None and hi[1] is not None else None
        own = _mid(tops.get(r.ticker), t)
        side = f["side"]
        res = results.get(r.ticker)
        if side == "yes":
            mid, bid, ask, rmid = yes_mid, yes_bid, yes_ask, own[2]
        else:
            mid = None if yes_mid is None else 1 - yes_mid
            bid = None if yes_ask is None else 1 - yes_ask
            ask = None if yes_bid is None else 1 - yes_bid
            rmid = None if own[2] is None else 1 - own[2]
        out.append(
            FillCheck(
                ticker=r.ticker,
                close_ts=r.close_ts,
                ts_ms=t,
                side=side,
                contracts=float(f["contracts"]),
                price=float(f["price"]),
                won=None if res not in ("yes", "no") else res == side,
                ladder_mid=mid,
                ladder_bid=bid,
                ladder_ask=ask,
                range_mid=rmid,
            )
        )
    return out


def fill_report(checks: Sequence[FillCheck]) -> dict[str, Any]:
    """Split each fill's P&L into price-vs-ladder and outcome-vs-ladder parts.

    pnl = (won - price) = (ladder_mid - price) + (won - ladder_mid). If the first part is
    near zero the range was priced in line with the ladder and the loss is the outcome.
    """
    rows = [c for c in checks if c.won is not None and c.ladder_mid is not None]
    if not rows:
        return {"fills": 0}
    w = np.array([c.contracts for c in rows])
    ev = np.array([c.close_ts for c in rows])
    price = np.array([c.price for c in rows])
    mid = np.array([c.ladder_mid for c in rows], dtype=float)
    won = np.array([1.0 if c.won else 0.0 for c in rows])
    parts = {"pnl": won - price, "price_vs_ladder": mid - price, "outcome_vs_ladder": won - mid}
    out: dict[str, Any] = {
        "fills": len(rows),
        "closes": len(set(ev.tolist())),
        "contracts": w.sum(),
    }
    for k, v in parts.items():
        # contract-weighted mean, CI resampling closes (repeat each fill by its size)
        rep = np.repeat(np.arange(len(v)), np.maximum(np.rint(w).astype(int), 1))
        lo, hi = event_ci(v[rep], ev[rep])
        out[k] = {
            "weighted_cents": float(np.average(v, weights=w) * 100),
            "ci_cents": [lo * 100, hi * 100],
            "unweighted_cents": float(v.mean() * 100),
        }
    inside = [
        c
        for c in rows
        if c.ladder_bid is not None
        and c.ladder_ask is not None
        and c.ladder_bid <= c.price <= c.ladder_ask
    ]
    out["fills_inside_ladder_spread"] = len(inside)
    out["fills_above_ladder_ask"] = sum(
        1 for c in rows if c.ladder_ask is not None and c.price > c.ladder_ask
    )
    out["biggest"] = [
        asdict(c)
        for c in sorted(rows, key=lambda c: -c.contracts * abs(c.price - float(bool(c.won))))[:5]
    ]
    return out


# ---------------------------------------------------------------- 1-minute candles


def candle_pass(history_dir: Path) -> dict[str, Any]:
    """Same comparison on Kalshi's 1-minute candles (close bid/ask of each minute).

    Candles are not synchronized books and carry no size: legs' quotes are only aligned
    to the minute and a gap here is a lead, not a measurement. Fees are per contract on
    a 100-lot.
    """
    from edgebook.history import kalshi as kh

    markets: dict[str, MarketInfo] = {}
    results: dict[str, str] = {}
    candles: dict[str, dict[int, tuple[float, float]]] = {}
    for series in (RANGE, LADDER):
        for m in kh.load_markets(history_dir, series):
            markets[m["ticker"]] = MarketInfo(
                m["ticker"],
                series,
                m["open_ts"],
                m["close_ts"],
                m["strike_type"] or "",
                m["floor"],
                m["cap"],
            )
            results[m["ticker"]] = m["result"]
        for tk, cs in kh.load_candles(history_dir, series).items():
            candles[tk] = {
                c["end_ts"]: (c["bid"], c["ask"])
                for c in cs
                if c["bid"] is not None and c["ask"] is not None and 0 < c["bid"] < c["ask"] < 1
            }
    legs, unmatched = match_ranges(markets)
    minutes = 0
    hits: dict[str, list[tuple[int, float]]] = {d: [] for d in DIRECTIONS}
    best: dict[int, float] = {}
    band: list[tuple[int, float, float, float]] = []  # (close, range ask, ladder mid, won)
    for r in legs:
        names = [r.ticker] + [x for x in (r.lower, r.upper) if x is not None]
        if any(n not in candles for n in names):
            continue
        common = set.intersection(*(set(candles[n]) for n in names))
        for ts in sorted(common):
            if ts >= r.close_ts:
                continue
            minutes += 1
            ask = candles[r.ticker][ts][1]
            if (
                LONGSHOT.min_ask <= ask <= LONGSHOT.max_ask
                and LONGSHOT.min_secs_left <= r.close_ts - ts <= LONGSHOT.max_secs_left
                and results.get(r.ticker) in ("yes", "no")
            ):
                lo = 1.0 if r.lower is None else sum(candles[r.lower][ts]) / 2
                hi = 0.0 if r.upper is None else sum(candles[r.upper][ts]) / 2
                band.append((r.close_ts, ask, lo - hi, float(results[r.ticker] == "yes")))
            for d, basket in baskets(r).items():
                cost = 0.0
                for tk, side in basket:
                    bid, ask = candles[tk][ts]
                    px = ask if side == "yes" else 1 - bid
                    cost += px + taker_fee(px)
                edge = basket_payoff(r, d) - cost
                best[r.close_ts] = max(best.get(r.close_ts, -math.inf), edge)
                if edge > 0:
                    hits[d].append((r.close_ts, edge))
    closes = sorted({r.close_ts for r in legs})
    out: dict[str, Any] = {
        "ranges_matched": len(legs),
        "ranges_unmatched": len(unmatched),
        "boundary_check": check_results(legs, results),
        "closes": len(closes),
        "range_minutes": minutes,
        "best_edge_cents_per_close": _q([v * 100 for v in best.values()], (0.1, 0.5, 0.9, 1.0)),
    }
    for d, hs in hits.items():
        per: dict[int, int] = defaultdict(int)
        for c, _ in hs:
            per[c] += 1
        out[d] = {
            "minutes_with_gap": len(hs),
            "closes_with_gap": len(per),
            "gap_minutes_per_close": _boot_mean([per.get(c, 0) for c in best]),
            "edge_cents": _q([e * 100 for _, e in hs]),
        }
    out["longshot_band"] = longshot_band(band)
    return out


def longshot_band(rows: Sequence[tuple[int, float, float, float]]) -> dict[str, Any]:
    """Range YES in the long-shot band: its ask vs the ladder-implied mid vs the outcome.

    Each row is one range-minute (close, range YES ask, ladder YES mid, won 0/1). The
    paper seller sells this YES at its ask: ``won - ask`` is the buyer's P&L, so a
    negative value is the seller's profit. CIs resample closes.
    """
    if not rows:
        return {"minutes": 0}
    a = np.array(rows, dtype=float)
    ev, ask, mid, won = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    out: dict[str, Any] = {"minutes": len(a), "closes": len(np.unique(ev))}
    for k, v in {
        "range_ask": ask,
        "ladder_mid": mid,
        "won": won,
        "ladder_mid_minus_ask": mid - ask,
        "won_minus_ask": won - ask,
        "won_minus_ladder_mid": won - mid,
    }.items():
        lo, hi = event_ci(v, ev)
        out[k] = {"cents": float(v.mean() * 100), "ci_cents": [lo * 100, hi * 100]}
    return out


# ---------------------------------------------------------------- entry point


def analyse(
    data_dir: Path, paper: Path | None = None, history_dir: Path | None = None
) -> dict[str, Any]:
    from edgebook.research.engine_replay import load_markets

    replay = load_markets(data_dir)
    markets = {tk: m.info for tk, m in replay.items()}
    results = {tk: m.result for tk, m in replay.items() if m.result in ("yes", "no")}
    legs, unmatched = match_ranges(markets)
    keep = {x for r in legs for x in (r.ticker, r.lower, r.upper) if x is not None}
    files = book_files(data_dir)
    gaps = outages(files)
    tops = top_of_book(files, keep, gaps)
    end_ms = max(int(t.ts[-1]) for t in tops.values())
    scans = [s for r in legs if (s := scan_range(r, tops, end_ms, gaps)) is not None]
    out: dict[str, Any] = {
        "data_dir": str(data_dir),
        "start_ms": min(int(t.ts[0]) for t in tops.values()),
        "end_ms": end_ms,
        "recorder_outages": [{"from_ms": a, "to_ms": b, "secs": (b - a) / 1000} for a, b in gaps],
        "ranges_matched": len(legs),
        "ranges_unmatched": len(unmatched),
        "unmatched_examples": unmatched[:10],
        "boundary_check": check_results(legs, results),
        "books": summarize(scans),
        "top_episodes": [
            asdict(e)
            for e in sorted((e for s in scans for e in s.episodes), key=lambda e: -e.dollars)[:15]
        ],
        "per_close": per_close(scans),
    }
    if paper is not None and paper.exists():
        fills = json.loads(paper.read_text())["fills"]
        by_ticker = {r.ticker: r for r in legs}
        out["longshot_fills"] = fill_report(check_fills(fills, by_ticker, tops, results))
        settled = [f for f in fills if f["action"] == "SETTLE" and f["ticker"].startswith("KXBTC-")]
        n = sum(f["contracts"] for f in settled)
        out["longshot_fills"]["account_settled"] = {
            "records": len(settled),
            "weighted_cents": 100 * sum(f["pnl"] for f in settled) / n if n else None,
        }
    if history_dir is not None:
        out["candles"] = candle_pass(history_dir)
    return out


def main() -> None:  # pragma: no cover - research entry point
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data_dir", type=Path, help="recorder Parquet root (book/, markets/...)")
    parser.add_argument("--paper", type=Path, help="paper account JSON with longshot fills")
    parser.add_argument("--history", type=Path, help="1-minute candle backfill (KXBTC+KXBTCD)")
    parser.add_argument("--out", type=Path, default=Path("reports/range_ladder.json"))
    args = parser.parse_args()
    res = analyse(args.data_dir, args.paper, args.history)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2, default=float))
    print(
        json.dumps(
            {k: v for k, v in res.items() if k not in ("per_close", "top_episodes")},
            indent=2,
            default=float,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()
