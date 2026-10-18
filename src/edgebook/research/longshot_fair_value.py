"""Does the live engine's fair value pick better long-shot sales than the current rule?

The paper long-shot strategy (``engine.maker.longshot_quote``) sells a 2-15c long shot by
resting a bid on the favourite, unless its fair value (20% BRTI model blended into Kalshi's
mid in log-odds) says the long shot is worth at least its ask. So the current rule already
uses fair value with a margin of zero: it sells when ``edge = long_ask - fair_long > 0``,
which is the same as "fair value of the favourite exceeds the price paid".

This module rebuilds every moment that rule fired from the engine's decision logs
(``engine.loop._log_decisions``: every 10 s, the primary strategy's calls), recomputes the
long-shot strategy's fair value from the logged ``model_up`` and ``market_up``, joins
Kalshi's official results and tests one pre-registered rule: sell only when the edge is
at least ``m``, with ``m`` chosen on the earliest third of closes and evaluated once on the
later closes. Policies take one entry per market (the strategy holds to the result) and
intervals resample whole closes, since every strike of one hour moves together.

Two subsets are reported: every logged firing (priced at the long shot's ask, as if it
filled) and the markets the paper account actually bought (its real fill P&L).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from edgebook.engine.decide import blend_fair
from edgebook.engine.maker import LongshotConfig
from edgebook.research.longshot_gaps import PAPER_MODEL_WEIGHT, event_ci, seller_pnl

MARGINS = (0.0, 0.0025, 0.005, 0.0075, 0.01)  # pre-registered grid for m, $ per contract
MIN_CALIBRATION_MARKETS = 10
FILL_LOOKBACK_MS = 600_000  # a paper fill uses the latest logged firing in the 10 min before it
N_BOOT = 2000


@dataclass(frozen=True)
class LoggedFiring:
    """One decision-log row where the long-shot band and time window apply."""

    event: str
    ticker: str
    ts_ms: int
    close_ms: int
    long_is_up: bool
    long_ask: float
    fair_long: float  # the long-shot strategy's fair value of the long shot
    edge: float  # long_ask - fair_long = favourite fair value - price paid


@dataclass(frozen=True)
class Entry:
    """One market a policy sells (one entry per market, held to the result)."""

    event: str
    ticker: str
    close_ms: int
    edge: float
    pnl: float  # $ per contract to the seller


def firing_from_row(
    row: Mapping[str, Any],
    cfg: LongshotConfig | None = None,
    weight: float = PAPER_MODEL_WEIGHT,
) -> LoggedFiring | None:
    """The long-shot sale this logged call offers, before the fair-value check (or None).

    Mirrors ``longshot_quote``: UP is checked first (its ask), then DOWN (1 - UP bid).
    Rows from the long-shot strategy's own log (``engine.loop.longshot_log_rows``) carry
    its fair value already and are used as logged, when the data checks passed.
    """
    cfg = cfg or LongshotConfig()
    secs = row.get("seconds_left")
    if "fair_long" in row:
        if not row.get("data_ok") or secs is None:
            return None
        if not cfg.min_secs_left <= secs <= cfg.max_secs_left:
            return None
        return _firing(row, bool(row["long_is_up"]), row["long_ask"], row["fair_long"], secs)
    up_ask, up_bid = row.get("up_ask"), row.get("up_bid")
    model, market = row.get("model_up"), row.get("market_up")
    if secs is None or up_ask is None or up_bid is None or model is None or market is None:
        return None
    if not cfg.min_secs_left <= secs <= cfg.max_secs_left:
        return None
    fair_up = blend_fair(model, market, weight)
    down_ask = round(1 - up_bid, 4)
    if cfg.min_ask <= up_ask <= cfg.max_ask:
        up, long_ask, fair_long = True, float(up_ask), fair_up
    elif cfg.min_ask <= down_ask <= cfg.max_ask:
        up, long_ask, fair_long = False, down_ask, 1 - fair_up
    else:
        return None
    return _firing(row, up, long_ask, fair_long, secs)


def _firing(
    row: Mapping[str, Any], up: bool, long_ask: float, fair_long: float, secs: float
) -> LoggedFiring:
    ticker = str(row["ticker"])
    ts = int(row["ts_ms"])
    return LoggedFiring(
        event=ticker.rsplit("-", 1)[0],
        ticker=ticker,
        ts_ms=ts,
        close_ms=round((ts + secs * 1000) / 60_000) * 60_000,  # closes fall on whole minutes
        long_is_up=up,
        long_ask=float(long_ask),
        fair_long=float(fair_long),
        edge=float(long_ask) - float(fair_long),
    )


def load_firings(paths: Iterable[Path], cfg: LongshotConfig | None = None) -> list[LoggedFiring]:
    out = []
    for path in paths:
        with path.open() as fh:
            for line in fh:
                f = firing_from_row(json.loads(line), cfg)
                if f is not None:
                    out.append(f)
    return sorted(out, key=lambda f: f.ts_ms)


def won_long(long_is_up: bool, result: str) -> bool:
    return (result == "yes") == long_is_up


def policy_entries(
    firings: Sequence[LoggedFiring], results: Mapping[str, str], margin: float | None
) -> list[Entry]:
    """Earliest settled firing per market with edge > margin (None = no fair-value check)."""
    chosen: dict[str, Entry] = {}
    for f in sorted(firings, key=lambda f: f.ts_ms):
        res = results.get(f.ticker)
        if res not in ("yes", "no") or f.ticker in chosen:
            continue
        if margin is not None and not f.edge > margin:
            continue
        pnl = seller_pnl(f.long_ask, won_long(f.long_is_up, res))
        chosen[f.ticker] = Entry(f.event, f.ticker, f.close_ms, f.edge, pnl)
    return list(chosen.values())


def paper_entries(account: Mapping[str, Any], firings: Sequence[LoggedFiring]) -> list[Entry]:
    """The paper account's settled markets that have a logged firing on the same side in
    the ``FILL_LOOKBACK_MS`` before the first fill; P&L is the account's own, per contract."""
    first_buy: dict[str, dict[str, Any]] = {}
    settle: dict[str, dict[str, Any]] = {}
    for fill in account["fills"]:
        if fill["action"] == "BUY":
            prev = first_buy.get(fill["ticker"])
            if prev is None or fill["ts_ms"] < prev["ts_ms"]:
                first_buy[fill["ticker"]] = fill
        elif fill["action"] == "SETTLE" and fill.get("pnl") is not None:
            settle[fill["ticker"]] = fill
    by_ticker: dict[str, list[LoggedFiring]] = defaultdict(list)
    for f in firings:
        by_ticker[f.ticker].append(f)
    out = []
    for tk, buy in first_buy.items():
        s = settle.get(tk)
        if s is None or not s["contracts"]:
            continue
        long_is_up = buy["side"] == "no"  # a DOWN bid sells the UP long shot
        prior = [
            f
            for f in by_ticker.get(tk, [])
            if f.long_is_up == long_is_up and 0 <= buy["ts_ms"] - f.ts_ms <= FILL_LOOKBACK_MS
        ]
        if not prior:
            continue
        f = max(prior, key=lambda f: f.ts_ms)
        out.append(Entry(f.event, tk, f.close_ms, f.edge, s["pnl"] / s["contracts"]))
    return out


def split_events(entries: Sequence[Entry]) -> tuple[set[str], set[str]]:
    """(earliest third, later two thirds) of closes by close time."""
    first: dict[str, int] = {}
    for e in entries:
        first[e.event] = min(first.get(e.event, e.close_ms), e.close_ms)
    closes = sorted((ms, ev) for ev, ms in first.items())
    cut = len(closes) // 3
    return {ev for _, ev in closes[:cut]}, {ev for _, ev in closes[cut:]}


def choose_margin(
    by_margin: Mapping[float, Sequence[Entry]], min_markets: int = MIN_CALIBRATION_MARKETS
) -> float | None:
    """The margin with the best mean per-contract P&L (ties -> smaller), or None."""
    best: tuple[float, float] | None = None
    for m in sorted(by_margin):
        rows = by_margin[m]
        if len(rows) < min_markets:
            continue
        mean = float(np.mean([e.pnl for e in rows]))
        if best is None or mean > best[1] + 1e-12:
            best = (m, mean)
    return None if best is None else best[0]


def bar(entries: Sequence[Entry], seed: int = 0) -> dict[str, Any]:
    """The bankroll-bar numbers: effect per contract with a 95% interval resampling closes,
    trades per day and worst close (sum of 1-contract P&L in one close)."""
    if not entries:
        return {"trades": 0, "closes": 0}
    pnl = np.array([e.pnl for e in entries])
    ev = np.array([e.event for e in entries])
    lo, hi = event_ci(pnl, ev, n_boot=N_BOOT, seed=seed)
    per_close: dict[str, float] = defaultdict(float)
    for e in entries:
        per_close[e.event] += e.pnl
    closes = [e.close_ms for e in entries]
    days = (max(closes) - min(closes) + 3_600_000) / 86_400_000
    worst = min(per_close, key=lambda k: per_close[k])
    return {
        "trades": len(entries),
        "closes": len(per_close),
        "mean_cents": float(pnl.mean() * 100),
        "ci_cents": [lo * 100, hi * 100],
        "trades_per_day": len(entries) / days,
        "worst_close": worst,
        "worst_close_cents": per_close[worst] * 100,
    }


def diff_ci(
    policy: Sequence[Entry], base: Sequence[Entry], seed: int = 0, n_boot: int = N_BOOT
) -> dict[str, Any]:
    """Mean per-contract P&L of ``policy`` minus ``base``, with a 95% interval from
    resampling whole closes (the same draw of closes for both)."""
    events = sorted({e.event for e in base} | {e.event for e in policy})
    if not events or not policy or not base:
        return {"diff_cents": None, "ci_cents": [None, None]}
    index = {ev: i for i, ev in enumerate(events)}

    def sums(rows: Sequence[Entry]) -> tuple[np.ndarray, np.ndarray]:
        s, n = np.zeros(len(events)), np.zeros(len(events))
        for e in rows:
            s[index[e.event]] += e.pnl
            n[index[e.event]] += 1
        return s, n

    ps, pn = sums(policy)
    bs, bn = sums(base)
    point = ps.sum() / pn.sum() - bs.sum() / bn.sum()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(events), size=(n_boot, len(events)))
    with np.errstate(invalid="ignore", divide="ignore"):
        draws = ps[idx].sum(1) / pn[idx].sum(1) - bs[idx].sum(1) / bn[idx].sum(1)
    draws = draws[np.isfinite(draws)]
    lo, hi = np.percentile(draws, [2.5, 97.5]) if len(draws) else (math.nan, math.nan)
    return {"diff_cents": float(point * 100), "ci_cents": [float(lo * 100), float(hi * 100)]}


def verdict(diff: Mapping[str, Any], eval_closes: int, min_closes: int = 30) -> str:
    """Pre-registered: YES if the interval is above zero; NO if it is below zero, or the
    point estimate is not positive on enough closes; otherwise NOT YET."""
    d, (lo, hi) = diff["diff_cents"], diff["ci_cents"]
    if d is None:
        return "NOT YET"
    if lo > 0:
        return "YES"
    if hi < 0 or (d <= 0 and eval_closes >= min_closes):
        return "NO"
    return "NOT YET"


def evaluate(
    firings: Sequence[LoggedFiring],
    results: Mapping[str, str],
    entries_for: Any,
) -> dict[str, Any]:
    """Pick m on the earliest third of closes, then evaluate once on the rest.

    ``entries_for(margin)`` returns the entries a margin keeps (None = band only); the
    close split is taken from the current rule's entries so every policy shares it.
    """
    current = entries_for(0.0)
    early, late = split_events(current)
    grid = {m: [e for e in entries_for(m) if e.event in early] for m in MARGINS}
    m_star = choose_margin(grid)
    calibration = {
        f"{m * 100:.2f}c": {
            "trades": len(rows),
            "mean_cents": float(np.mean([e.pnl for e in rows]) * 100) if rows else None,
        }
        for m, rows in grid.items()
    }
    out: dict[str, Any] = {
        "calibration_closes": len(early),
        "evaluation_closes": len(late),
        "calibration_by_margin": calibration,
        "chosen_margin_cents": None if m_star is None else m_star * 100,
    }
    late_current = [e for e in current if e.event in late]
    late_band = [e for e in entries_for(None) if e.event in late]
    out["evaluation"] = {"current_rule": bar(late_current), "band_only": bar(late_band)}
    out["current_minus_band_only"] = diff_ci(late_current, late_band)
    if m_star is None:
        out["verdict"] = "NOT YET"
        out["verdict_reason"] = "too few calibration markets to choose m"
        return out
    late_policy = [e for e in entries_for(m_star) if e.event in late]
    out["evaluation"]["policy"] = bar(late_policy)
    diff = diff_ci(late_policy, late_current)
    out["policy_minus_current"] = diff
    out["verdict"] = verdict(diff, len(late))
    return out


def analyse(
    firings: Sequence[LoggedFiring], results: Mapping[str, str], account: Mapping[str, Any]
) -> dict[str, Any]:
    settled = [f for f in firings if results.get(f.ticker) in ("yes", "no")]
    paper = paper_entries(account, settled)

    def paper_for(m: float | None) -> list[Entry]:
        return [e for e in paper if m is None or e.edge > m]

    edges = np.array([f.edge for f in settled])
    return {
        "logged_firings": len(firings),
        "settled_firings": len(settled),
        "markets": len({f.ticker for f in settled}),
        "first_ts_ms": min((f.ts_ms for f in firings), default=None),
        "last_ts_ms": max((f.ts_ms for f in firings), default=None),
        "edge_percentiles_cents": (
            dict(
                zip(
                    ("p5", "p25", "p50", "p75", "p95"),
                    (np.percentile(edges, [5, 25, 50, 75, 95]) * 100).tolist(),
                    strict=True,
                )
            )
            if len(edges)
            else {}
        ),
        "all_firings": evaluate(settled, results, lambda m: policy_entries(settled, results, m)),
        "paper_filled": {"matched_trades": len(paper), **evaluate(settled, results, paper_for)},
    }


def fetch_results(tickers: Iterable[str], cache: Path) -> dict[str, str]:  # pragma: no cover
    """Kalshi's official result per ticker (public REST), cached; unsettled markets omitted."""
    import asyncio

    from edgebook.config import KALSHI_URLS
    from edgebook.venues.kalshi.rest import KalshiRest

    known: dict[str, str] = json.loads(cache.read_text()) if cache.exists() else {}
    todo = sorted(set(tickers) - set(known))

    async def run() -> None:
        rest = KalshiRest(KALSHI_URLS["production"])
        try:
            for tk in todo:
                m = await rest.market(tk)
                if m.result in ("yes", "no"):
                    known[tk] = m.result
        finally:
            await rest.aclose()

    asyncio.run(run())
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(known, indent=1, sort_keys=True))
    return known


def main() -> None:  # pragma: no cover - research entry point
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("decisions", type=Path, nargs="+", help="decision-log .jsonl files")
    parser.add_argument("--account", type=Path, required=True, help="paper-longshot.json")
    parser.add_argument("--results", type=Path, default=Path("reports/kalshi-results.json"))
    parser.add_argument("--out", type=Path, default=Path("reports/longshot-fair-value.json"))
    args = parser.parse_args()
    firings = load_firings(args.decisions)
    account = json.loads(args.account.read_text())
    tickers = {f.ticker for f in firings} | {
        f["ticker"] for f in account["fills"] if f["action"] == "BUY"
    }
    results = fetch_results(tickers, args.results)
    result = analyse(firings, results, account)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
