"""Do BTC options imply hourly settlement odds that Kalshi's strike ladder gets wrong?

Kalshi's hourly ``KXBTCD`` event lists a ladder of "BTC above K at the top of the hour"
strikes, settled on the 60-second average of the CF Benchmarks BRTI. At any moment its mids
trace out a distribution. Fitting ``probit(mid) = a - b * ln(K / S)`` across the strikes gives
that distribution two numbers:

- **vol** ``sigma_k = 1 / (b * sqrt(tau))``: how wide Kalshi thinks the next tau minutes are;
- **level** ``a`` in standard deviations: where Kalshi centres it relative to spot.

Deribit's BTC options give an independent vol: ATM implied volatility of the front daily
expiry, inverted from the mark prices of trades in the previous ``IV_WINDOW_S``. A
lognormal, driftless probability with that vol and Coinbase spot (a BRTI constituent) is the
options view. Swapping only the vol, or only the level, into Kalshi's fit separates a *vol*
disagreement from a *level* disagreement.

Point-in-time: a Kalshi candle, spot minute, DVOL minute or option trade is only used if it
ended at or before the decision time. Labels are Kalshi's official results.

The trading test is pre-registered: ``select_rule`` sees only the earliest third of closes
and its choice is evaluated once on the later two thirds, one entry per market, with 95%
intervals from resampling whole closes (``by_close``).

Run ``uv run python -m edgebook.research.options_implied --kalshi-dir <dir> --deribit-dir
<dir>``; it writes ``reports/options-implied.json``.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from scipy.optimize import brentq
from scipy.stats import norm

from edgebook.core.types import LiquidityRole
from edgebook.engine.decide import fee, fill_fee
from edgebook.history.kalshi import tradeable
from edgebook.research.deribit import DVOL_FILE, TRADES_FILE, Option, parse_instrument

YEAR_S = 365.0 * 24 * 3600
SETTLE_AVG_S = 60  # Kalshi settles on the BRTI average of the final minute
HORIZONS_MIN = tuple(range(60, 14, -5))  # 60, 55, ..., 15 minutes before close
IV_WINDOW_S = 1800  # option trades used for the ATM IV at a decision time
ATM_BAND = 0.02  # |ln(K / index)| for a trade to count as at-the-money
MIN_EXPIRY_S = 3600  # skip an expiry closer than this (its IV is noise)
MIN_IV_TRADES = 3
FILL_WINDOW_MIN = 10  # maker fill proxy: a print at or through our bid within this
SIZE = 100  # contracts per fill for the fee (Kalshi rounds each fill's total up once)
VARIANTS = ("opt", "opt_seasonal", "dvol")
THRESHOLDS = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08)
ROLES = (LiquidityRole.TAKER, LiquidityRole.MAKER)
MIN_TRAIN_TRADES = 50


# ---------------------------------------------------------------- probability model


def effective_tau(secs_left: float) -> float:
    """Variance time to settlement in years.

    The settle value averages the final 60 s, whose variance is a third of a point price's,
    so the last minute counts as 20 s.
    """
    return max(secs_left - SETTLE_AVG_S + SETTLE_AVG_S / 3, 1.0) / YEAR_S


def p_above(spot: float, strike: float, sigma: float, tau: float, shift_sd: float = 0.0) -> float:
    """Driftless lognormal P(S_T > K), with an optional centre shift in standard deviations."""
    sd = sigma * math.sqrt(tau)
    z = (math.log(spot / strike) - sd * sd / 2) / sd + shift_sd
    return float(norm.cdf(z))


@dataclass(frozen=True)
class LadderFit:
    sigma: float  # annualised vol Kalshi's ladder implies
    shift_sd: float  # Kalshi's centre minus spot (driftless), in standard deviations
    n: int


def fit_ladder(
    spot: float, strikes: Sequence[float], mids: Sequence[float], tau: float
) -> LadderFit | None:
    """OLS of probit(mid) on ln(K / S) across one event's tradeable strikes."""
    if len(strikes) < 3:
        return None
    x = np.log(np.asarray(strikes) / spot)
    z = norm.ppf(np.clip(np.asarray(mids), 0.005, 0.995))
    if float(np.ptp(x)) <= 0:
        return None
    slope, intercept = np.polyfit(x, z, 1)
    b = -float(slope)
    if b <= 0:
        return None
    sd = 1 / b
    sigma = sd / math.sqrt(tau)
    # intercept = (ln F/S - sd^2/2) / sd; the driftless lognormal has -sd/2 there
    return LadderFit(sigma, float(intercept) + sd / 2, len(strikes))


# ---------------------------------------------------------------- options vol


def bs_price(fwd: float, strike: float, sigma: float, t: float, call: bool) -> float:
    sd = sigma * math.sqrt(t)
    d1 = (math.log(fwd / strike) + sd * sd / 2) / sd
    d2 = d1 - sd
    if call:
        return float(fwd * norm.cdf(d1) - strike * norm.cdf(d2))
    return float(strike * norm.cdf(-d2) - fwd * norm.cdf(-d1))


def implied_vol(price: float, fwd: float, strike: float, t: float, call: bool) -> float | None:
    """Black-76 IV with zero rates (Deribit premiums are in BTC: pass price * index)."""
    intrinsic = max(fwd - strike, 0.0) if call else max(strike - fwd, 0.0)
    if t <= 0 or price <= intrinsic + 1e-9:
        return None
    try:
        root: float = brentq(lambda s: bs_price(fwd, strike, s, t, call) - price, 0.01, 6.0)
    except ValueError:
        return None
    return root


@dataclass
class OptionTrades:
    """Option trades sorted by time with parsed instruments, for ATM IV lookups."""

    ts: np.ndarray  # Unix s (float)
    opt: list[Option]
    mark: np.ndarray  # BTC
    index: np.ndarray
    iv_trade: np.ndarray  # Deribit's IV at the fill, decimal
    _mark_iv: dict[int, float | None] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: Sequence[dict[str, Any]]) -> OptionTrades:
        keep = []
        for r in sorted(rows, key=lambda r: int(r["ts_ms"])):
            o = parse_instrument(r["instrument"])
            if o is None or r["mark_price"] is None or r["index_price"] is None:
                continue
            keep.append((r, o))
        return cls(
            ts=np.array([int(r["ts_ms"]) / 1000 for r, _ in keep], dtype=float),
            opt=[o for _, o in keep],
            mark=np.array([float(r["mark_price"]) for r, _ in keep]),
            index=np.array([float(r["index_price"]) for r, _ in keep]),
            iv_trade=np.array([(r["iv"] or float("nan")) / 100 for r, _ in keep]),
        )

    def mark_iv(self, i: int) -> float | None:
        if i not in self._mark_iv:
            o, s = self.opt[i], float(self.index[i])
            t = (o.expiry_ts - float(self.ts[i])) / YEAR_S
            self._mark_iv[i] = implied_vol(float(self.mark[i]) * s, s, o.strike, t, o.call)
        return self._mark_iv[i]

    def atm(self, t: int) -> tuple[float, int, float] | None:
        """(median ATM mark IV, its expiry, latest index) of the front usable expiry."""
        lo = int(np.searchsorted(self.ts, t - IV_WINDOW_S, side="left"))
        hi = int(np.searchsorted(self.ts, t, side="right"))
        by_expiry: dict[int, list[int]] = defaultdict(list)
        for i in range(lo, hi):
            o = self.opt[i]
            if o.expiry_ts - t < MIN_EXPIRY_S:
                continue
            if abs(math.log(o.strike / float(self.index[i]))) <= ATM_BAND:
                by_expiry[o.expiry_ts].append(i)
        for expiry in sorted(by_expiry):
            ivs = [v for i in by_expiry[expiry] if (v := self.mark_iv(i)) is not None]
            if len(ivs) >= MIN_IV_TRADES:
                return float(np.median(ivs)), expiry, float(self.index[by_expiry[expiry][-1]])
        return None


@dataclass(frozen=True)
class Series:
    """A point-in-time 1-minute series: value of the last point at or before t."""

    ts: np.ndarray
    val: np.ndarray
    max_age_s: int = 120

    def at(self, t: float) -> float | None:
        i = int(np.searchsorted(self.ts, t, side="right")) - 1
        if i < 0 or t - float(self.ts[i]) > self.max_age_s:
            return None
        return float(self.val[i])


def hourly_weights(spot: Series, until_ts: int) -> np.ndarray:
    """Relative 1-minute return variance by UTC hour, from spot before ``until_ts``."""
    mask = spot.ts < until_ts
    ts, logp = spot.ts[mask], np.log(spot.val[mask])
    ok = np.diff(ts) == 60
    r2 = np.diff(logp)[ok] ** 2
    hours = ((ts[1:][ok] - 60) // 3600 % 24).astype(int)
    w = np.array([r2[hours == h].mean() if (hours == h).any() else np.nan for h in range(24)])
    w = np.where(np.isnan(w), np.nanmean(w), w)
    out: np.ndarray = w / w.mean()
    return out


def seasonal_factor(weights: np.ndarray, t: int, close: int, expiry: int) -> float:
    """sqrt(mean hourly weight over [t, close] / mean over [t, expiry]).

    An option's IV averages variance until its expiry; the hour to the Kalshi close may be a
    quiet or busy part of the day.
    """

    def mean_w(a: int, b: int) -> float:
        mins = np.arange(a, max(b, a + 60), 60)
        return float(weights[(mins // 3600 % 24).astype(int)].mean())

    return math.sqrt(mean_w(t, close) / mean_w(t, expiry))


# ---------------------------------------------------------------- dataset


@dataclass(frozen=True)
class Row:
    """One market at one decision time."""

    event: str
    ticker: str
    close_ts: int
    minutes_left: int
    strike: float
    spot: float
    bid: float
    ask: float
    yes: bool
    sigma_k: float | None  # Kalshi ladder vol
    shift_k: float | None  # Kalshi ladder level shift (sd)
    sigma: dict[str, float]  # options vols by variant
    deribit_index: float | None
    future: tuple[tuple[int, float, float, float | None, float], ...]  # maker fill proxy

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def tau(self) -> float:
        return effective_tau(self.minutes_left * 60)

    def p(self, variant: str) -> float | None:
        s = self.sigma.get(variant)
        return None if s is None else p_above(self.spot, self.strike, s, self.tau())


@dataclass
class Data:
    rows: list[Row]
    basis_bps: dict[str, list[float]]


def _candles(path: Path) -> dict[str, list[tuple[int, float, float, float | None, float]]]:
    c = pq.read_table(path, columns=["ticker", "end_ts", "bid", "ask", "last", "volume"])
    d = c.to_pydict()
    by: dict[str, list[tuple[int, float, float, float | None, float]]] = defaultdict(list)
    for tk, end, bid, ask, last, vol in zip(
        d["ticker"], d["end_ts"], d["bid"], d["ask"], d["last"], d["volume"], strict=True
    ):
        by[tk].append((end, bid, ask, last, vol or 0.0))
    for rows in by.values():
        rows.sort(key=lambda r: r[0])
    return by


def _quote(
    rows: list[tuple[int, float, float, float | None, float]], t: int
) -> tuple[int, float, float] | None:
    i = bisect.bisect_right([r[0] for r in rows], t) - 1
    if i < 0 or t - rows[i][0] > 120:
        return None
    _, bid, ask, _, _ = rows[i]
    return (i, bid, ask) if tradeable(bid, ask) else None


def build(kalshi_dir: Path, deribit_dir: Path, series: str = "KXBTCD") -> Data:
    markets = pq.read_table(kalshi_dir / f"kalshi_markets_{series}.parquet").to_pylist()
    candles = _candles(kalshi_dir / f"kalshi_candles_{series}.parquet")
    s = pq.read_table(kalshi_dir / "spot_1m_BTC-USD.parquet").to_pydict()
    order = np.argsort(s["ts"])
    spot = Series(np.asarray(s["ts"])[order], np.asarray(s["close"], dtype=float)[order])
    dv = pq.read_table(deribit_dir / DVOL_FILE).to_pydict()
    dvol = Series(np.asarray(dv["ts"]), np.asarray(dv["dvol"], dtype=float) / 100)
    trades = OptionTrades.from_rows(pq.read_table(deribit_dir / TRADES_FILE).to_pylist())

    closes = sorted({m["close_ts"] for m in markets})
    train_end = closes[len(closes) // 3]
    weights = hourly_weights(spot, train_end - 3600)

    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in markets:
        if m["result"] in ("yes", "no") and m["floor"] is not None:
            by_event[m["event_ticker"]].append(m)

    rows: list[Row] = []
    basis: dict[str, list[float]] = {"brti_vs_coinbase": [], "deribit_vs_coinbase": []}
    for event, ms in sorted(by_event.items(), key=lambda kv: kv[1][0]["close_ts"]):
        close = ms[0]["close_ts"]
        settle, cb = ms[0]["expiration_value"], spot.at(close)
        if settle and cb:
            basis["brti_vs_coinbase"].append(1e4 * math.log(settle / cb))
        for h in HORIZONS_MIN:
            t = close - h * 60
            s_t = spot.at(t)
            if s_t is None:
                continue
            tau = effective_tau(h * 60)
            quotes = []
            for m in ms:
                if t < m["open_ts"]:
                    continue
                cs = candles.get(m["ticker"], [])
                q = _quote(cs, t)
                if q is not None:
                    quotes.append((m, q, cs))
            if not quotes:
                continue
            fit = fit_ladder(
                s_t,
                [m["floor"] for m, _, _ in quotes],
                [(b + a) / 2 for _, (_, b, a), _ in quotes],
                tau,
            )
            sig: dict[str, float] = {}
            atm = trades.atm(t)
            idx = None
            if atm is not None:
                iv, expiry, idx = atm
                sig["opt"] = iv
                sig["opt_seasonal"] = iv * seasonal_factor(weights, t, close, expiry)
                basis["deribit_vs_coinbase"].append(1e4 * math.log(idx / s_t))
            d = dvol.at(t)
            if d is not None:
                sig["dvol"] = d
            for m, (i, bid, ask), cs in quotes:
                rows.append(
                    Row(
                        event=event,
                        ticker=m["ticker"],
                        close_ts=close,
                        minutes_left=h,
                        strike=float(m["floor"]),
                        spot=s_t,
                        bid=bid,
                        ask=ask,
                        yes=m["result"] == "yes",
                        sigma_k=fit.sigma if fit else None,
                        shift_k=fit.shift_sd if fit else None,
                        sigma=sig,
                        deribit_index=idx,
                        future=tuple(c for c in cs[i + 1 :] if c[0] - t <= FILL_WINDOW_MIN * 60),
                    )
                )
    return Data(rows, basis)


def split(rows: Sequence[Row]) -> tuple[list[Row], list[Row], int]:
    """Earliest third of closes (train) and the rest (test), plus the first test close."""
    closes = sorted({r.close_ts for r in rows})
    cut = closes[len(closes) // 3]
    return [r for r in rows if r.close_ts < cut], [r for r in rows if r.close_ts >= cut], cut


# ---------------------------------------------------------------- comparison


def _scores(p: np.ndarray, y: np.ndarray) -> dict[str, float]:
    pc = np.clip(p, 1e-4, 1 - 1e-4)
    return {
        "brier": float(np.mean((pc - y) ** 2)),
        "log_loss": float(-np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc))),
    }


def compare(rows: Sequence[Row]) -> dict[str, Any]:
    """Forecast scores on rows where every forecast exists, plus the vol/level split.

    - ``kalshi_mid``: the market
    - ``kalshi_fit``: the two-number ladder fit (how much the fit itself loses)
    - ``vol_swap``: Kalshi's level, options vol (a pure vol disagreement)
    - ``level_swap``: spot as the centre, Kalshi's vol (a pure level disagreement)
    - ``opt`` / ``opt_seasonal`` / ``dvol``: the options views
    """
    usable = [r for r in rows if r.sigma_k is not None and all(v in r.sigma for v in VARIANTS)]
    if not usable:
        return {"n": 0}
    y = np.array([r.yes for r in usable], dtype=float)
    f: dict[str, list[float]] = defaultdict(list)
    for r in usable:
        tau = r.tau()
        sk, shk = r.sigma_k or 0.0, r.shift_k or 0.0
        f["kalshi_mid"].append(r.mid)
        f["kalshi_fit"].append(p_above(r.spot, r.strike, sk, tau, shk))
        so = r.sigma["opt_seasonal"]
        # keep Kalshi's centre in price units when the width changes
        shift = shk * sk / so
        f["vol_swap"].append(p_above(r.spot, r.strike, so, tau, shift))
        f["level_swap"].append(p_above(r.spot, r.strike, sk, tau))
        for v in VARIANTS:
            f[v].append(r.p(v) or 0.0)
    scores = {k: _scores(np.array(v), y) for k, v in f.items()}
    ev = {}
    for r in usable:
        ev[(r.event, r.minutes_left)] = r
    pts = list(ev.values())
    ratio = {
        v: _summary([math.log(r.sigma[v] / (r.sigma_k or 1.0)) for r in pts]) for v in VARIANTS
    }
    return {
        "n_rows": len(usable),
        "n_snapshots": len(pts),
        "scores": scores,
        "log_vol_ratio_options_over_kalshi": ratio,
        "kalshi_vol": _summary([r.sigma_k or 0.0 for r in pts]),
        "kalshi_level_shift_sd": _summary([r.shift_k or 0.0 for r in pts]),
        "options_vol": {v: _summary([r.sigma[v] for r in pts]) for v in VARIANTS},
    }


def _summary(x: Sequence[float]) -> dict[str, float]:
    a = np.asarray(x, dtype=float)
    if a.size == 0:
        return {"n": 0}
    return {
        "n": int(a.size),
        "mean": float(a.mean()),
        "sd": float(a.std(ddof=1)) if a.size > 1 else float("nan"),
        "p10": float(np.percentile(a, 10)),
        "p50": float(np.percentile(a, 50)),
        "p90": float(np.percentile(a, 90)),
    }


def realized_check(rows: Sequence[Row], settle: dict[int, float]) -> dict[str, float]:
    """Std of ln(settle / spot) / (sigma sqrt(tau)) per vol source: 1.0 = right width."""
    out: dict[str, list[float]] = defaultdict(list)
    seen = set()
    for r in rows:
        key = (r.event, r.minutes_left)
        if key in seen or r.close_ts not in settle or r.sigma_k is None:
            continue
        seen.add(key)
        move = math.log(settle[r.close_ts] / r.spot)
        sq = math.sqrt(r.tau())
        out["kalshi"].append(move / (r.sigma_k * sq))
        for v in VARIANTS:
            if v in r.sigma:
                out[v].append(move / (r.sigma[v] * sq))
    return {k: float(np.std(v, ddof=1)) for k, v in out.items() if len(v) > 1}


# ---------------------------------------------------------------- trading rule


@dataclass(frozen=True)
class Rule:
    variant: str
    threshold: float  # minimum edge after fee, $/contract
    role: str  # "taker" | "maker"


@dataclass(frozen=True)
class Trade:
    event: str
    ticker: str
    close_ts: int
    minutes_left: int
    side: str
    price: float
    edge: float
    pnl: float


def _fee(price: float, role: LiquidityRole) -> float:
    return fill_fee(price, SIZE, role) / SIZE


def maker_filled(row: Row, side: str) -> bool:
    """Our bid for ``side`` rests at the current bid; did the market reach it in 10 min?

    Buying YES at the YES bid b fills when YES prints <= b or the ask drops to b. Buying NO
    at 1 - ask (the NO bid) fills when YES prints >= ask or the bid rises to it.
    """
    for _, bid, ask, last, vol in row.future:
        px = last if vol > 0 and last is not None else None
        if side == "yes" and ((px is not None and px <= row.bid + 1e-9) or ask <= row.bid + 1e-9):
            return True
        if side == "no" and ((px is not None and px >= row.ask - 1e-9) or bid >= row.ask - 1e-9):
            return True
    return False


def signal(row: Row, rule: Rule) -> Trade | None:
    p = row.p(rule.variant)
    if p is None:
        return None
    role = LiquidityRole.TAKER if rule.role == "taker" else LiquidityRole.MAKER
    if role is LiquidityRole.TAKER:
        yes_px, no_px = row.ask, round(1 - row.bid, 4)
    else:
        yes_px, no_px = row.bid, round(1 - row.ask, 4)
    best: Trade | None = None
    for side, prob, px in (("yes", p, yes_px), ("no", 1 - p, no_px)):
        if not 0 < px < 1:
            continue
        edge = prob - px - _fee(px, role)
        if edge >= rule.threshold and (best is None or edge > best.edge):
            won = row.yes if side == "yes" else not row.yes
            pnl = (1.0 if won else 0.0) - px - _fee(px, role)
            best = Trade(row.event, row.ticker, row.close_ts, row.minutes_left, side, px, edge, pnl)
    if best is not None and role is LiquidityRole.MAKER and not maker_filled(row, best.side):
        return None
    return best


def run(rows: Sequence[Row], rule: Rule) -> list[Trade]:
    """First firing per market, scanning decision times from 60 down to 15 minutes."""
    by: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by[r.ticker].append(r)
    out = []
    for rs in by.values():
        for r in sorted(rs, key=lambda r: -r.minutes_left):
            tr = signal(r, rule)
            if tr is not None:
                out.append(tr)
                break
    return out


@dataclass(frozen=True)
class Verdict:
    """The bankroll bar: effect per contract with a 95% interval resampling closes."""

    trades: int
    closes_traded: int
    closes_total: int
    days: float
    cents_per_contract: float
    ci_lo: float
    ci_hi: float
    trades_per_day: float
    worst_close_dollars: float  # sum of one contract per trade at the worst close
    best_close_dollars: float
    win_rate: float


def by_close(
    trades: Sequence[Trade], closes: Sequence[int], n_boot: int = 4000, seed: int = 7
) -> Verdict:
    """Per-contract mean with a close-cluster bootstrap (closes without trades included)."""
    days = (max(closes) - min(closes) + 3600) / 86400 if closes else 0.0
    if not trades:
        return Verdict(0, 0, len(closes), days, *(float("nan"),) * 3, 0.0, 0.0, 0.0, float("nan"))
    pnl_by: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        pnl_by[t.close_ts].append(t.pnl)
    cs = sorted(set(closes) | set(pnl_by))
    sums = np.array([sum(pnl_by.get(c, [])) for c in cs])
    counts = np.array([len(pnl_by.get(c, [])) for c in cs])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(cs), size=(n_boot, len(cs)))
    num, den = sums[idx].sum(axis=1), counts[idx].sum(axis=1)
    boot = num[den > 0] / den[den > 0]
    traded = sums[counts > 0]
    return Verdict(
        trades=len(trades),
        closes_traded=int((counts > 0).sum()),
        closes_total=len(cs),
        days=days,
        cents_per_contract=100 * float(sums.sum() / counts.sum()),
        ci_lo=100 * float(np.percentile(boot, 2.5)),
        ci_hi=100 * float(np.percentile(boot, 97.5)),
        trades_per_day=len(trades) / days if days else float("nan"),
        worst_close_dollars=float(traded.min()),
        best_close_dollars=float(traded.max()),
        win_rate=float(np.mean([t.pnl > 0 for t in trades])),
    )


def grid() -> list[Rule]:
    return [Rule(v, x, r.value) for v in VARIANTS for x in THRESHOLDS for r in ROLES]


def select_rule(train: Sequence[Row]) -> tuple[Rule, list[dict[str, Any]]]:
    """The pre-registered choice: best train cents/contract with >= MIN_TRAIN_TRADES."""
    closes = sorted({r.close_ts for r in train})
    table = []
    for rule in grid():
        v = by_close(run(train, rule), closes, n_boot=500)
        table.append({**asdict(rule), **asdict(v)})
    ok = [t for t in table if t["trades"] >= MIN_TRAIN_TRADES]
    best = max(ok or table, key=lambda t: t["cents_per_contract"])
    return Rule(best["variant"], best["threshold"], best["role"]), table


# ---------------------------------------------------------------- report


def train_report(data: Data) -> dict[str, Any]:
    """Everything that may be looked at before the rule is registered: train closes only."""
    train, _, cut = split(data.rows)
    chosen, table = select_rule(train)
    return {
        "train_rows": len(train),
        "first_test_close": cut,
        "basis_bps": {k: _summary(v) for k, v in data.basis_bps.items()},
        "coverage": {v: float(np.mean([v in r.sigma for r in train])) for v in VARIANTS},
        "compare_train": compare(train),
        "train_grid": table,
        "selected_on_train": asdict(chosen),
    }


def report(data: Data, rule: Rule | None = None) -> dict[str, Any]:
    train, test, cut = split(data.rows)
    chosen, table = select_rule(train)
    registered = rule or chosen
    test_closes = sorted({r.close_ts for r in test})
    verdict = by_close(run(test, registered), test_closes)
    one_contract = Rule(registered.variant, registered.threshold, registered.role)
    rounding = _with_fee(test, one_contract, test_closes)
    return {
        "rows": len(data.rows),
        "train_rows": len(train),
        "test_rows": len(test),
        "first_test_close": cut,
        "basis_bps": {k: _summary(v) for k, v in data.basis_bps.items()},
        "coverage": {v: float(np.mean([v in r.sigma for r in data.rows])) for v in VARIANTS},
        "compare_train": compare(train),
        "compare_test": compare(test),
        "train_grid": table,
        "selected_on_train": asdict(chosen),
        "registered": asdict(registered),
        "test_verdict": asdict(verdict),
        "test_verdict_1_contract_fee": asdict(rounding),
        "test_by_horizon": _by_horizon(run(test, registered)),
    }


def _with_fee(rows: Sequence[Row], rule: Rule, closes: Sequence[int]) -> Verdict:
    """The registered rule with Kalshi's 1-contract fee rounding (worst case)."""
    role = LiquidityRole.TAKER if rule.role == "taker" else LiquidityRole.MAKER
    trades = [
        Trade(**{**asdict(t), "pnl": t.pnl + _fee(t.price, role) - fee(t.price, role)})
        for t in run(rows, rule)
    ]
    return by_close(trades, closes)


def _by_horizon(trades: Sequence[Trade]) -> dict[int, dict[str, float]]:
    by: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        by[t.minutes_left].append(t.pnl)
    return {h: {"n": len(v), "cents": 100 * float(np.mean(v))} for h, v in sorted(by.items())}


def _settles(kalshi_dir: Path, series: str) -> dict[int, float]:
    ms = pq.read_table(kalshi_dir / f"kalshi_markets_{series}.parquet").to_pylist()
    return {m["close_ts"]: m["expiration_value"] for m in ms if m["expiration_value"]}


def main(argv: list[str] | None = None, out: Callable[[str], None] = print) -> dict[str, Any]:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kalshi-dir", type=Path, default=Path("history"))
    ap.add_argument("--deribit-dir", type=Path, default=Path("history"))
    ap.add_argument("--out", type=Path, default=Path("reports/options-implied.json"))
    ap.add_argument("--rule", help="registered rule as variant:threshold:role")
    ap.add_argument("--train-only", action="store_true", help="never touch the test closes")
    args = ap.parse_args(argv)
    data = build(args.kalshi_dir, args.deribit_dir)
    settle = _settles(args.kalshi_dir, "KXBTCD")
    if args.train_only:
        rep = train_report(data)
        rep["realized_width_train"] = realized_check(split(data.rows)[0], settle)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rep, indent=2, default=str))
        out(json.dumps(rep["selected_on_train"]))
        return rep
    rule = None
    if args.rule:
        v, x, role = args.rule.split(":")
        rule = Rule(v, float(x), role)
    rep = report(data, rule)
    train, test, _ = split(data.rows)
    rep["realized_width_train"] = realized_check(train, settle)
    rep["realized_width_test"] = realized_check(test, settle)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rep, indent=2, default=str))
    out(json.dumps({k: rep[k] for k in ("registered", "test_verdict")}, indent=2))
    return rep


if __name__ == "__main__":  # pragma: no cover
    main()
