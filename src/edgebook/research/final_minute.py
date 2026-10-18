"""Final-minute nowcast: does exact knowledge of the partial settlement average pay?

Kalshi BTC contracts (KXBTC15M, KXBTCD) settle on the simple average of the 60
BRTI prints timed in ``[close - 60s, close)``. Inside that window part of the
average is already printed, so only the remainder is uncertain. This module
replays the recorder's Parquet streams to test whether a taker who knows the
partial average beats Kalshi's quotes after fees, and how fast the order path
must be to keep the edge.

Clocks. Everything runs on the recorder's receipt clock (``recv_ts_ms``). A
decision at ``D`` uses the BRTI prints and the book received by ``D``. An order
"arriving after delay d" executes against the book the recorder had received by
``D + d``. Because the recorder itself sees the exchange one feed hop late, a
real order path of one-way latency ``L`` corresponds to ``d ~= L + feed lag``
(see ``latency``).

Fill model: buy at the best ask with the ask as the limit; at arrival the order
walks the recorded opposite ladder up to that limit (partial fills from depth),
pays Kalshi's taker fee rounded up per fill level, and holds to the official
result. One attempt per market, at the earliest checkpoint whose edge clears
the threshold.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from scipy import stats

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.research.longshot_gaps import event_ci

SERIES = ("KXBTC15M", "KXBTCD")
WINDOW_MS = 60_000
PRINT_MS = 1_000
CHECKPOINTS_S = (60, 45, 30, 15, 5)
DELAYS_MS = (0, 100, 300, 500, 1000, 2000)
THRESHOLDS = tuple(c / 100 for c in (0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30))
VOL_LOOKBACK_S = 900
MIN_VOL_PER_S = 2e-5  # floor on per-second log vol so quiet stretches don't look certain
MAX_ORDER = 25  # contracts per attempt; smaller if less is shown at the limit
TICKS = 10_000  # price grid: 0.0001 dollars
DAY_MS = 86_400_000


# ------------------------------------------------------------------ loading


def parquet_files(root: Path, stream: str) -> list[Path]:
    """Non-empty part files of a stream, in write order (``part-<first_ms>-<seq>``)."""

    def key(p: Path) -> tuple[int, int]:
        m = re.match(r"part-(\d+)-(\d+)\.parquet$", p.name)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    files = [p for p in (root / stream).rglob("*.parquet") if p.stat().st_size > 0]
    return sorted(files, key=key)


def load_table(root: Path, stream: str, columns: Sequence[str] | None = None) -> pa.Table:
    tables = [pq.read_table(p, columns=columns) for p in parquet_files(root, stream)]
    if not tables:
        raise FileNotFoundError(f"no {stream} data under {root}")
    return pa.concat_tables(tables)


@dataclass(frozen=True)
class Contract:
    ticker: str
    series: str
    close_ms: int
    strike: float
    strike_type: str  # greater | greater_or_equal
    result: str  # yes | no
    expiration_value: float | None


def load_contracts(root: Path, series: Sequence[str] = SERIES) -> dict[str, Contract]:
    """Settled single-strike contracts with an official yes/no result."""
    out: dict[str, Contract] = {}
    for r in load_table(root, "settlements").to_pylist():
        if r["series"] not in series or r["result"] not in ("yes", "no"):
            continue
        if r["strike_type"] not in ("greater", "greater_or_equal") or r["floor_strike"] is None:
            continue
        ev = r["expiration_value"]
        out[r["ticker"]] = Contract(
            ticker=r["ticker"],
            series=r["series"],
            close_ms=int(r["close_time"].timestamp() * 1000),
            strike=float(r["floor_strike"]),
            strike_type=r["strike_type"],
            result=r["result"],
            expiration_value=float(ev) if ev not in (None, "") else None,
        )
    return out


@dataclass(frozen=True)
class Prints:
    """BRTI prints, one per print time, sorted by print time."""

    time_ms: np.ndarray  # CF Benchmarks print time (whole seconds)
    value: np.ndarray
    kalshi_ms: np.ndarray  # when Kalshi says it received the print
    recv_ms: np.ndarray  # when the recorder received Kalshi's message

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[int, float, int, int]]) -> Prints:
        best: dict[int, tuple[int, float, int, int]] = {}
        for row in rows:
            seen = best.get(row[0])
            if seen is None or row[3] < seen[3]:  # keep the first receipt of each print
                best[row[0]] = row
        ordered = sorted(best.values())
        arr = np.array(ordered, dtype=np.float64).reshape(-1, 4)
        return cls(
            time_ms=arr[:, 0].astype(np.int64),
            value=arr[:, 1],
            kalshi_ms=arr[:, 2].astype(np.int64),
            recv_ms=arr[:, 3].astype(np.int64),
        )


def load_prints(root: Path, index_id: str = "BRTI") -> Prints:
    t = load_table(root, "index", ["recv_ts_ms", "received_at_ms", "index_id", "raw"])
    rows = []
    for recv, kal, iid, raw in zip(
        t["recv_ts_ms"].to_pylist(),
        t["received_at_ms"].to_pylist(),
        t["index_id"].to_pylist(),
        t["raw"].to_pylist(),
        strict=True,
    ):
        if iid != index_id:
            continue
        msg = json.loads(raw)
        rows.append((int(msg["time"]), float(msg["value"]), int(kal), int(recv)))
    return Prints.from_rows(rows)


# ------------------------------------------------------------------ settlement and nowcast


def settles_yes(avg: float, strike: float, strike_type: str) -> bool:
    return avg >= strike if strike_type == "greater_or_equal" else avg > strike


def settlement_average(prints: Prints, close_ms: int) -> tuple[float, int]:
    """Mean of prints timed in [close - 60s, close), and how many there were."""
    lo = int(np.searchsorted(prints.time_ms, close_ms - WINDOW_MS, "left"))
    hi = int(np.searchsorted(prints.time_ms, close_ms, "left"))
    if hi <= lo:
        return math.nan, 0
    return float(prints.value[lo:hi].mean()), hi - lo


@dataclass(frozen=True)
class Nowcast:
    known_n: int  # window prints received by the decision time
    known_avg: float  # their mean (nan if none)
    remaining_n: int  # window prints still to come
    last: float  # latest print received
    sigma: float  # per-second log vol used
    mean: float  # expected final average
    sd: float  # its standard deviation

    def prob_yes(self, strike: float, nu: float | None = None) -> float:
        if self.sd <= 0:
            return 1.0 if self.mean >= strike else 0.0
        z = (self.mean - strike) / self.sd
        if nu is None:
            return float(stats.norm.cdf(z))
        return float(stats.t.cdf(z * math.sqrt(nu / (nu - 2)), nu))


def realized_sigma(prints: Prints, end: int, lookback_s: int = VOL_LOOKBACK_S) -> float:
    """Per-second log vol from consecutive 1 s prints in the ``lookback_s`` before index ``end``."""
    lo = max(0, end - lookback_s)
    t, v = prints.time_ms[lo:end], prints.value[lo:end]
    if len(v) < 30:
        return MIN_VOL_PER_S
    step = np.diff(t) == PRINT_MS
    r = np.diff(np.log(v))[step]
    if len(r) < 30:
        return MIN_VOL_PER_S
    return max(float(np.sqrt(np.mean(r * r))), MIN_VOL_PER_S)


def nowcast(prints: Prints, close_ms: int, decision_ms: int) -> Nowcast | None:
    """Known part of the window plus a random-walk forecast of the prints still to come.

    The remaining ``n`` prints are ``S0 * exp(sigma * W_j)`` for ``j = 1..n`` after the last
    received print; their sum has variance ``S0^2 sigma^2 n(n+1)(2n+1)/6`` (to first order).
    """
    received = prints.recv_ms <= decision_ms
    if not received.any():
        return None
    # Last print received by the decision (prints arrive in time order, so use the max time).
    last_i = int(np.flatnonzero(received)[-1])
    last_t = int(prints.time_ms[last_i])
    if decision_ms - last_t > 10_000:
        return None  # feed stalled; no usable state
    start = close_ms - WINDOW_MS
    in_win = received & (prints.time_ms >= start) & (prints.time_ms < close_ms)
    k = int(in_win.sum())
    known_sum = float(prints.value[in_win].sum())
    last_slot = max(last_t, start - PRINT_MS)
    n = max(0, (close_ms - PRINT_MS - last_slot) // PRINT_MS)
    s0 = float(prints.value[last_i])
    sigma = realized_sigma(prints, last_i + 1)
    total = k + n
    if total == 0:
        return None
    mean = (known_sum + n * s0) / total
    var_sum = (s0 * sigma) ** 2 * n * (n + 1) * (2 * n + 1) / 6
    return Nowcast(
        known_n=k,
        known_avg=known_sum / k if k else math.nan,
        remaining_n=int(n),
        last=s0,
        sigma=sigma,
        mean=mean,
        sd=math.sqrt(var_sum) / total,
    )


# ------------------------------------------------------------------ book replay


@dataclass(frozen=True)
class Ladder:
    """Resting bids per side, best first: (price, qty)."""

    yes: tuple[tuple[float, float], ...]
    no: tuple[tuple[float, float], ...]

    @property
    def yes_bid(self) -> float | None:
        return self.yes[0][0] if self.yes else None

    @property
    def yes_ask(self) -> float | None:
        return round(1 - self.no[0][0], 4) if self.no else None

    @property
    def crossed(self) -> bool:
        b, a = self.yes_bid, self.yes_ask
        return b is not None and a is not None and b >= a

    def ask(self, side: str) -> tuple[float, float] | None:
        """(best price, size) to buy ``side``."""
        opp = self.no if side == "yes" else self.yes
        return (round(1 - opp[0][0], 4), opp[0][1]) if opp else None

    def depth_at(self, side: str, limit: float) -> float:
        opp = self.no if side == "yes" else self.yes
        return sum(q for p, q in opp if 1 - p <= limit + 1e-9)


def _ladder(state: np.ndarray) -> Ladder:
    def side(row: np.ndarray) -> tuple[tuple[float, float], ...]:
        idx = np.flatnonzero(row > 1e-9)[::-1]
        return tuple((i / TICKS, float(row[i])) for i in idx)

    return Ladder(yes=side(state[0]), no=side(state[1]))


@dataclass
class _Chunk:
    recv: np.ndarray
    tick: np.ndarray  # ticker code into ``names``
    reset: np.ndarray  # bool
    side: np.ndarray  # 0 yes, 1 no
    price: np.ndarray  # grid index
    qty: np.ndarray
    names: list[str]


def _chunks(root: Path, tickers: set[str]) -> Iterator[_Chunk]:
    want = pa.array(sorted(tickers))
    cols = ["recv_ts_ms", "kind", "ticker", "side", "price", "qty"]
    for p in parquet_files(root, "book"):
        t = pq.read_table(p, columns=cols)
        t = t.filter(pc.is_in(t["ticker"], value_set=want))
        if t.num_rows == 0:
            continue
        enc = pc.dictionary_encode(t["ticker"]).combine_chunks()
        kind = t["kind"].to_numpy(zero_copy_only=False)
        price = pc.fill_null(pc.cast(t["price"], pa.float64()), 0.0).to_numpy()
        qty = pc.fill_null(pc.cast(t["qty"], pa.float64()), 0.0).to_numpy()
        side = t["side"].to_numpy(zero_copy_only=False)
        recv = t["recv_ts_ms"].to_numpy()
        order = np.argsort(recv, kind="stable")
        yield _Chunk(
            recv=recv[order],
            tick=enc.indices.to_numpy()[order],
            reset=(kind == "reset")[order],
            side=(side == "no").astype(np.int64)[order],
            price=np.rint(price * TICKS).astype(np.int64)[order],
            qty=qty[order],
            names=enc.dictionary.to_pylist(),
        )


class _Books:
    def __init__(self) -> None:
        self.state: dict[str, np.ndarray] = {}
        self.ready: set[str] = set()

    def apply(self, c: _Chunk, lo: int, hi: int) -> None:
        if hi <= lo:
            return
        codes = c.tick[lo:hi]
        order = np.argsort(codes, kind="stable")
        sc = codes[order]
        bounds = np.flatnonzero(np.diff(sc)) + 1
        for grp in np.split(order, bounds):
            rows = grp + lo
            name = c.names[int(c.tick[rows[0]])]
            resets = np.flatnonzero(c.reset[rows])
            st = self.state.get(name)
            if len(resets):
                st = np.zeros((2, TICKS + 1))
                self.state[name] = st
                self.ready.add(name)
                rows = rows[resets[-1] + 1 :]
            elif st is None:
                st = np.zeros((2, TICKS + 1))
                self.state[name] = st
            keep = ~c.reset[rows]
            rows = rows[keep]
            np.add.at(st, (c.side[rows], c.price[rows]), c.qty[rows])

    def ladder(self, name: str) -> Ladder | None:
        st = self.state.get(name)
        if st is None or name not in self.ready:
            return None
        return _ladder(st)

    def drop(self, name: str) -> None:
        self.state.pop(name, None)
        self.ready.discard(name)


def book_snapshots(
    root: Path, queries: dict[str, Sequence[int]], chunks: Iterable[_Chunk] | None = None
) -> dict[tuple[str, int], Ladder]:
    """Ladder of each ticker as received by each query time (recv clock).

    Replays book resets/levels/deltas in arrival order; a ticker is usable only
    after its first snapshot reset. State is dropped after the ticker's last query.
    """
    flat = sorted((t, name) for name, ts in queries.items() for t in ts)
    last_q = {name: max(ts) for name, ts in queries.items() if ts}
    out: dict[tuple[str, int], Ladder] = {}
    books = _Books()
    qi = 0
    source = chunks if chunks is not None else _chunks(root, set(queries))
    for c in source:
        pos = 0
        end_t = int(c.recv[-1])
        while qi < len(flat) and flat[qi][0] < end_t:
            qt = flat[qi][0]
            cut = int(np.searchsorted(c.recv, qt, "right"))
            books.apply(c, pos, cut)
            pos = cut
            while qi < len(flat) and flat[qi][0] == qt:
                name = flat[qi][1]
                lad = books.ladder(name)
                if lad is not None:
                    out[(name, qt)] = lad
                if last_q.get(name) == qt:
                    books.drop(name)
                qi += 1
        books.apply(c, pos, len(c.recv))
    return out


# ------------------------------------------------------------------ execution


def taker_fee(price: float, contracts: float = 1.0) -> float:
    p = min(max(price, 0.0001), 0.9999)
    return float(trade_fee(Decimal(f"{contracts:.2f}"), Decimal(f"{p:.4f}"), LiquidityRole.TAKER))


def fee_per_contract(price: float) -> float:
    """Unrounded taker fee per contract (what a multi-contract order pays on average)."""
    return 0.07 * price * (1 - price)


@dataclass(frozen=True)
class Execution:
    filled: float
    avg_price: float
    fees: float


def execute_buy(ladder: Ladder | None, side: str, limit: float, qty: float) -> Execution:
    """Walk the opposite ladder at prices <= ``limit``; fee rounded up per level."""
    if ladder is None or ladder.crossed:
        return Execution(0.0, math.nan, 0.0)
    opp = ladder.no if side == "yes" else ladder.yes
    left, cost, fees = qty, 0.0, 0.0
    for p, q in opp:
        price = round(1 - p, 4)
        if price > limit + 1e-9 or left <= 1e-9:
            break
        take = math.floor(min(q, left) * 100) / 100
        if take <= 0:
            continue
        cost += take * price
        fees += taker_fee(price, take)
        left -= take
    filled = qty - left
    return Execution(filled, cost / filled if filled > 0 else math.nan, fees)


# ------------------------------------------------------------------ experiment


@dataclass
class Signal:
    """One market at one checkpoint."""

    ticker: str
    series: str
    close_ms: int
    checkpoint_s: int
    p_yes: float
    won_yes: bool
    yes_bid: float | None
    yes_ask: float | None
    side: str  # side with the larger edge
    edge: float  # nowcast edge per contract after the unrounded taker fee
    limit: float
    shown: float  # size at the limit when deciding
    executions: dict[int, Execution] = field(default_factory=dict)


@dataclass(frozen=True)
class Entry:
    ticker: str
    close_ms: int
    checkpoint_s: int
    side: str
    edge: float
    filled: float
    pnl_per_contract: float  # nan if nothing filled


def build_signals(
    contracts: dict[str, Contract],
    prints: Prints,
    ladders: dict[tuple[str, int], Ladder],
    checkpoints: Sequence[int] = CHECKPOINTS_S,
    delays: Sequence[int] = DELAYS_MS,
    nu: float | None = None,
) -> list[Signal]:
    casts: dict[tuple[int, int], Nowcast | None] = {}
    out: list[Signal] = []
    for c in sorted(contracts.values(), key=lambda c: (c.close_ms, c.ticker)):
        for t in checkpoints:
            d = c.close_ms - t * 1000
            key = (c.close_ms, t)
            if key not in casts:
                casts[key] = nowcast(prints, c.close_ms, d)
            nc = casts[key]
            lad = ladders.get((c.ticker, d))
            if nc is None or lad is None or lad.crossed:
                continue
            p = nc.prob_yes(c.strike, nu) if c.strike_type == "greater_or_equal" else (
                nc.prob_yes(c.strike + 1e-6, nu)
            )
            best: tuple[float, str, float, float] | None = None
            for side, fair in (("yes", p), ("no", 1 - p)):
                quote = lad.ask(side)
                if quote is None or not 0 < quote[0] < 1:
                    continue
                edge = fair - quote[0] - fee_per_contract(quote[0])
                if best is None or edge > best[0]:
                    best = (edge, side, quote[0], lad.depth_at(side, quote[0]))
            if best is None:
                continue
            edge, side, limit, shown = best
            sig = Signal(
                ticker=c.ticker,
                series=c.series,
                close_ms=c.close_ms,
                checkpoint_s=t,
                p_yes=p,
                won_yes=c.result == "yes",
                yes_bid=lad.yes_bid,
                yes_ask=lad.yes_ask,
                side=side,
                edge=edge,
                limit=limit,
                shown=shown,
            )
            qty = float(min(MAX_ORDER, max(1.0, math.floor(shown))))
            for dl in delays:
                sig.executions[dl] = execute_buy(ladders.get((c.ticker, d + dl)), side, limit, qty)
            out.append(sig)
    return out


def entries(signals: Sequence[Signal], threshold: float, delay_ms: int) -> list[Entry]:
    """One attempt per market: the earliest checkpoint whose edge clears ``threshold``."""
    first: dict[str, Signal] = {}
    for s in signals:
        if s.edge < threshold:
            continue
        cur = first.get(s.ticker)
        if cur is None or s.checkpoint_s > cur.checkpoint_s:
            first[s.ticker] = s
    out = []
    for s in first.values():
        ex = s.executions[delay_ms]
        won = s.won_yes == (s.side == "yes")
        pnl = (
            (1.0 if won else 0.0) - ex.avg_price - ex.fees / ex.filled
            if ex.filled > 0
            else math.nan
        )
        out.append(Entry(s.ticker, s.close_ms, s.checkpoint_s, s.side, s.edge, ex.filled, pnl))
    return out


def bar(rows: Sequence[Entry], span_days: float) -> dict[str, Any]:
    """Bankroll-bar numbers: per-contract effect with a close-resampled 95% CI, trades/day,
    worst close (sum of per-contract P&L of the markets in one close)."""
    filled = [r for r in rows if r.filled > 0]
    pnl = np.array([r.pnl_per_contract for r in filled])
    ev = np.array([r.close_ms for r in filled])
    lo, hi = event_ci(pnl, ev)
    by_close: dict[int, float] = defaultdict(float)
    for r in filled:
        by_close[r.close_ms] += r.pnl_per_contract
    return {
        "attempts": len(rows),
        "filled": len(filled),
        "fill_rate": len(filled) / len(rows) if rows else None,
        "closes": len(by_close),
        "mean_cents": float(pnl.mean() * 100) if len(pnl) else None,
        "ci_cents": [lo * 100, hi * 100],
        "trades_per_day": len(filled) / span_days if span_days > 0 else None,
        "worst_close_dollars": min(by_close.values()) if by_close else None,
        "contracts_per_fill": float(np.mean([r.filled for r in filled])) if filled else None,
    }


def split_closes(signals: Sequence[Signal], frac: float = 1 / 3) -> tuple[set[int], set[int]]:
    closes = sorted({s.close_ms for s in signals})
    cut = int(len(closes) * frac)
    return set(closes[:cut]), set(closes[cut:])


def choose_threshold(
    signals: Sequence[Signal], delay_ms: int = 0, min_fills: int = 20
) -> tuple[float, list[dict[str, Any]]]:
    """Threshold with the largest total per-contract P&L (needs ``min_fills`` fills)."""
    table = []
    best_t, best_v = THRESHOLDS[-1], -math.inf
    for th in THRESHOLDS:
        e = [r for r in entries(signals, th, delay_ms) if r.filled > 0]
        total = float(sum(r.pnl_per_contract for r in e))
        table.append({"threshold_cents": th * 100, "fills": len(e), "total_dollars": total})
        if len(e) >= min_fills and total > best_v:
            best_t, best_v = th, total
    return best_t, table


def span_days(closes: Iterable[int]) -> float:
    cs = sorted(closes)
    return (cs[-1] - cs[0]) / DAY_MS if len(cs) > 1 else 0.0


def scorecard(signals: Sequence[Signal]) -> list[dict[str, Any]]:
    """Per checkpoint, on contested markets (0.03 < mid < 0.97): nowcast vs Kalshi mid
    Brier score, and the realized value of buying the nowcast's side at the ask."""
    out = []
    for t in sorted({s.checkpoint_s for s in signals}, reverse=True):
        rows = [s for s in signals if s.checkpoint_s == t and s.yes_bid and s.yes_ask]
        rows = [s for s in rows if 0.03 < ((s.yes_bid or 0) + (s.yes_ask or 0)) / 2 < 0.97]
        if not rows:
            continue
        y = np.array([1.0 if s.won_yes else 0.0 for s in rows])
        p = np.array([s.p_yes for s in rows])
        mid = np.array([((s.yes_bid or 0) + (s.yes_ask or 0)) / 2 for s in rows])
        spread = np.array([(s.yes_ask or 0) - (s.yes_bid or 0) for s in rows])
        val = []
        for s in rows:
            if s.edge < 0:
                continue
            won = s.won_yes == (s.side == "yes")
            val.append((1.0 if won else 0.0) - s.limit - fee_per_contract(s.limit))
        out.append(
            {
                "checkpoint_s": t,
                "markets": len(rows),
                "closes": len({s.close_ms for s in rows}),
                "brier_nowcast": float(np.mean((p - y) ** 2)),
                "brier_kalshi_mid": float(np.mean((mid - y) ** 2)),
                "mean_spread_cents": float(spread.mean() * 100),
                "positive_edge_markets": len(val),
                "mean_value_at_ask_cents": float(np.mean(val) * 100) if val else None,
            }
        )
    return out


# ------------------------------------------------------------------ latency


def _pcts(x: np.ndarray) -> dict[str, float]:
    if len(x) == 0:
        return {}
    q = np.percentile(x, [10, 50, 90, 99])
    return {"n": int(len(x)), "p10": q[0], "p50": q[1], "p90": q[2], "p99": q[3]}


def latency(root: Path, prints: Prints) -> dict[str, Any]:
    """Receipt lags of the recorder's own feeds (ms)."""
    lags: list[np.ndarray] = []
    for p in parquet_files(root, "book"):
        t = pq.read_table(p, columns=["recv_ts_ms", "sending_ts_ms"])
        s = t["sending_ts_ms"].to_numpy(zero_copy_only=False)
        r = t["recv_ts_ms"].to_numpy()
        ok = ~np.isnan(s.astype(np.float64))
        lags.append((r[ok] - s[ok].astype(np.int64))[::50])  # thin: ~187M rows
    book = np.concatenate(lags) if lags else np.array([])
    return {
        "brti_print_to_recorder": _pcts((prints.recv_ms - prints.time_ms).astype(float)),
        "brti_print_to_kalshi": _pcts((prints.kalshi_ms - prints.time_ms).astype(float)),
        "brti_kalshi_to_recorder": _pcts((prints.recv_ms - prints.kalshi_ms).astype(float)),
        "book_kalshi_send_to_recorder": _pcts(book.astype(float)),
    }


def settlement_check(contracts: dict[str, Contract], prints: Prints) -> dict[str, Any]:
    """Recompute each result from the recorded window; count disagreements."""
    agree = disagree = missing = 0
    worst_gap = 0.0
    by_close: dict[int, tuple[float, int]] = {}
    for c in contracts.values():
        if c.close_ms not in by_close:
            by_close[c.close_ms] = settlement_average(prints, c.close_ms)
        avg, n = by_close[c.close_ms]
        if n < 55:
            missing += 1
            continue
        if c.expiration_value is not None:
            worst_gap = max(worst_gap, abs(avg - c.expiration_value))
        if settles_yes(avg, c.strike, c.strike_type) == (c.result == "yes"):
            agree += 1
        else:
            disagree += 1
    return {
        "contracts_checked": agree + disagree,
        "agree": agree,
        "disagree": disagree,
        "missing_window": missing,
        "closes": len(by_close),
        "max_abs_gap_to_expiration_value": worst_gap,
    }


def analyse(root: Path, nu: float | None = None) -> dict[str, Any]:
    contracts = load_contracts(root)
    prints = load_prints(root)
    lo, hi = int(prints.time_ms[0]), int(prints.time_ms[-1])
    contracts = {
        k: c for k, c in contracts.items() if lo + VOL_LOOKBACK_S * 1000 < c.close_ms <= hi
    }
    queries = {
        k: sorted({c.close_ms - t * 1000 + dl for t in CHECKPOINTS_S for dl in DELAYS_MS})
        for k, c in contracts.items()
    }
    ladders = book_snapshots(root, queries)
    signals = build_signals(contracts, prints, ladders, nu=nu)
    train_c, test_c = split_closes(signals)
    train = [s for s in signals if s.close_ms in train_c]
    test = [s for s in signals if s.close_ms in test_c]
    threshold, train_table = choose_threshold(train)
    days = span_days(test_c)
    curve = {}
    for dl in DELAYS_MS:
        rows = entries(test, threshold, dl)
        curve[dl] = bar(rows, days)
        for series in SERIES:
            sub = [r for r in rows if contracts[r.ticker].series == series]
            curve[dl][series] = bar(sub, days)
    per_delay_threshold = {}
    for dl in DELAYS_MS:
        th, _ = choose_threshold(train, dl)
        per_delay_threshold[dl] = {"threshold_cents": th * 100, **bar(entries(test, th, dl), days)}
    return {
        "data": {
            "prints": len(prints.time_ms),
            "first_print_ms": lo,
            "last_print_ms": hi,
            "contracts": len(contracts),
            "signals": len(signals),
            "train_closes": len(train_c),
            "test_closes": len(test_c),
            "test_days": days,
        },
        "settlement_check": settlement_check(contracts, prints),
        "scorecard_all": scorecard(signals),
        "threshold_cents": threshold * 100,
        "train_thresholds": train_table,
        "delay_curve_test": curve,
        "threshold_rechosen_per_delay_test": per_delay_threshold,
        "latency_ms": latency(root, prints),
    }


def main() -> None:  # pragma: no cover - research entry point
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("data", type=Path, help="recorder snapshot root (book/, index/, ...)")
    ap.add_argument("--out", type=Path, default=Path("reports/final_minute.json"))
    ap.add_argument("--nu", type=float, default=None, help="Student-t tails for the nowcast")
    args = ap.parse_args()
    result = analyse(args.data, nu=args.nu)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, default=_json))
    print(json.dumps(result, indent=2, default=_json))


def _json(o: object) -> object:  # pragma: no cover
    if isinstance(o, np.generic):
        return o.item()
    if hasattr(o, "__dataclass_fields__"):
        return asdict(o)  # type: ignore[call-overload]
    raise TypeError(type(o))


if __name__ == "__main__":  # pragma: no cover
    main()
