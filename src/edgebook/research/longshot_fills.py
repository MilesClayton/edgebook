"""What precedes the long-shot seller's bad fills? A replay on the recorder's tick data.

The gap analysis (``research.longshot_gaps``) found the seller's edge lives only in orders
that never fill: filled long shots win about six times as often as unfilled ones. That study
used 1-minute candles and a fill proxy. This one replays the recorder's full order books and
trade prints through the *live* engine (``research.engine_replay``) running only the
``longshot`` strategy, so every order, re-post, queue position and fill comes from the
production code path (``engine.maker.quote_longshot`` + ``MakerBook.match``): a resting bid
joins the back of the queue at its price and fills only from prints that consume it.

While each order rests, one sample per second records what a cancel rule could see:
buyer-initiated long-shot volume, the long shot's bid stepping up, the queue at our price,
and the settlement index moving toward the long shot's strike.

A cancel rule is chosen on the earliest closes only (``choose_rule``), frozen in
``PREREGISTERED_RULE``, and evaluated once on later closes. Cancelling means standing aside
from that market for the rest of its life, so the rule can be scored on the same replay:
a fill is avoided when the rule fired at least ``CANCEL_LATENCY_MS`` before it.

Research sizing differs from the paper account in one way: per-close and open-market caps
are lifted so every eligible strike gets an order. Queue position does not depend on our
own size, so per-contract fill outcomes are unchanged.
"""

from __future__ import annotations

import bisect
import json
import math
import tempfile
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from edgebook.engine.loop import DEFAULT_STRATEGIES, LiveEngine, StrategySpec
from edgebook.engine.paper import RiskConfig
from edgebook.engine.state import INDEX_KEEP_S, LiveState
from edgebook.research.engine_replay import _apply_book, events, load_markets
from edgebook.research.longshot_gaps import Firing, seller_pnl, summary

SERIES = ("KXBTCD", "KXBTC15M")
FLOW_WINDOWS_S = (10, 30, 60, 300)
CANCEL_LATENCY_MS = 300  # decide at a tick, the cancel reaches Kalshi this much later
LEAD_S = (1, 10, 30, 60, 300)  # look-backs before a fill for the descriptive table
EXPLORE_SHARE = 0.5  # earliest share of closes used to choose the rule
MIN_KEPT_SHARE = 0.5  # a rule must keep at least this share of explore fills
RULE_FEATURES = (
    "buy_10",
    "buy_30",
    "buy_60",
    "buy_300",
    "bid_up_30",
    "bid_up_60",
    "level_drop_60",
    "toward_60",
    "toward_300",
)
RULE_QUANTILES = (0.9, 0.95, 0.98, 0.99, 0.995)


@dataclass(frozen=True)
class Rule:
    """Cancel (and stand aside) once ``feature >= threshold``."""

    feature: str
    threshold: float

    def fires(self, s: Sample) -> bool:
        v = getattr(s, self.feature)
        return v is not None and v >= self.threshold


# Chosen by ``choose_rule`` on the explore half of the 2026-10-16 recording, before the
# later closes were scored; see docs/LONGSHOT_ADVERSE_FILLS.md. Later data must be scored
# with this rule unchanged.
PREREGISTERED_RULE: Rule | None = None


@dataclass
class Sample:
    """What a cancel rule sees about one resting order at one tick (all about the long shot)."""

    t_ms: int
    secs_left: float
    price: float  # our resting bid (on the favourite)
    queue: float  # contracts ahead of us at our price
    level: float  # contracts resting at our price (others' orders)
    long_bid: float | None  # best bid for the long shot
    buy_10: float  # long-shot contracts bought by takers in the last N seconds
    buy_30: float
    buy_60: float
    buy_300: float
    sell_60: float  # long-shot contracts sold by takers
    bid_up_30: float | None  # long-shot bid change, cents (since placement if shorter)
    bid_up_60: float | None
    level_drop_60: float | None  # contracts that left our price level (+ = shrinking)
    toward_30: float | None  # index move toward the long shot paying out, bps
    toward_60: float | None
    toward_300: float | None
    dist_bps: float | None  # index move the long shot still needs, bps


@dataclass
class Episode:
    """Everything the strategy did on one market: first order to first fill (or close)."""

    ticker: str
    event: str
    close_ts: int
    side: str  # our side: the favourite
    placed_ms: int
    first_price: float
    queue_at_join: float
    reposts: int = 0
    first_fill_ms: int | None = None
    fill_price: float | None = None
    filled_contracts: float = 0.0
    result: str = ""
    samples: list[Sample] = field(default_factory=list)

    @property
    def filled(self) -> bool:
        return self.first_fill_ms is not None

    @property
    def long_side(self) -> str:
        return "no" if self.side == "yes" else "yes"

    @property
    def won_long(self) -> bool:
        return self.result == self.long_side

    @property
    def price(self) -> float:
        return self.fill_price if self.fill_price is not None else self.first_price

    @property
    def pnl(self) -> float:
        """$ per contract to the seller, at the fill price (or the first quote if unfilled)."""
        return seller_pnl(round(1 - self.price, 2), self.won_long)

    def firing(self) -> Firing:
        return Firing(
            self.event,
            self.ticker,
            self.close_ts,
            int((self.close_ts * 1000 - self.placed_ms) / 60_000),
            round(1 - self.price, 2),
            self.won_long,
            self.filled,
            self.pnl,
            {},
        )

    def before(self, t_ms: int) -> Sample | None:
        """The last sample whose data all arrived before ``t_ms``."""
        i = bisect.bisect_right(self.samples, t_ms, key=lambda s: s.t_ms) - 1
        return self.samples[i] if i >= 0 else None


# ------------------------------------------------------------------ replay


class _Tape:
    """Per-ticker trade prints and index history the engine state does not keep long enough."""

    def __init__(self) -> None:
        self.trades: dict[str, deque[tuple[int, float, str]]] = defaultdict(deque)
        self.index_ms: list[int] = []
        self.index_v: list[float] = []

    def add_trade(self, ticker: str, ts_ms: int, count: float, taker_side: str) -> None:
        q = self.trades[ticker]
        q.append((ts_ms, count, taker_side))
        while q and q[0][0] < ts_ms - max(FLOW_WINDOWS_S) * 1000:
            q.popleft()

    def flow(self, ticker: str, now_ms: int, secs: int, taker_side: str) -> float:
        lo = now_ms - secs * 1000
        return sum(
            c for ts, c, side in self.trades.get(ticker, ()) if ts >= lo and side == taker_side
        )

    def add_index(self, ts_ms: int, value: float) -> None:
        self.index_ms.append(ts_ms)
        self.index_v.append(value)

    def index_at(self, t_ms: int) -> float | None:
        i = bisect.bisect_right(self.index_ms, t_ms) - 1
        return self.index_v[i] if i >= 0 and t_ms - self.index_ms[i] <= 5_000 else None


def _toward_sign(strike_type: str, long_side: str) -> float | None:
    """+1 when a rising index helps the long shot pay out."""
    if strike_type.startswith("greater"):
        up_wins = 1.0
    elif strike_type.startswith("less"):
        up_wins = -1.0
    else:
        return None
    return up_wins if long_side == "yes" else -up_wins


def _sample(
    ep: Episode,
    state: LiveState,
    tape: _Tape,
    t: int,
    price: float,
    queue: float,
    sign: float | None,
    strike: float | None,
) -> Sample:
    tk, long_side, our = ep.ticker, ep.long_side, ep.side
    long_bids = state.bids(tk, long_side)
    long_bid = long_bids[0].price if long_bids else None
    level = sum(lv.size for lv in state.bids(tk, our) if abs(lv.price - price) < 1e-9)
    # A long-shot buyer is a taker on the long shot's side; those same prints fill us.
    buy = {n: tape.flow(tk, t, n, long_side) for n in FLOW_WINDOWS_S}

    def back(secs: int) -> Sample | None:
        if not ep.samples:
            return None
        s = ep.before(t - secs * 1000)
        return s if s is not None else ep.samples[0]

    def bid_up(secs: int) -> float | None:
        s = back(secs)
        if s is None or s.long_bid is None or long_bid is None:
            return None
        return round((long_bid - s.long_bid) * 100, 4)

    s60 = back(60)
    now_ix = tape.index_at(t)

    def toward(secs: int) -> float | None:
        then = tape.index_at(t - secs * 1000)
        if sign is None or now_ix is None or then is None:
            return None
        return sign * math.log(now_ix / then) * 1e4

    dist = None
    if sign is not None and now_ix is not None and strike:
        dist = sign * math.log(strike / now_ix) * 1e4
    return Sample(
        t_ms=t,
        secs_left=ep.close_ts - t / 1000,
        price=price,
        queue=queue,
        level=level,
        long_bid=long_bid,
        buy_10=buy[10],
        buy_30=buy[30],
        buy_60=buy[60],
        buy_300=buy[300],
        sell_60=tape.flow(tk, t, 60, our),
        bid_up_30=bid_up(30),
        bid_up_60=bid_up(60),
        level_drop_60=(s60.level - level) if s60 is not None and s60.price == price else None,
        toward_30=toward(30),
        toward_60=toward(60),
        toward_300=toward(300),
        dist_bps=dist,
    )


def research_spec() -> StrategySpec:
    """The paper ``longshot`` strategy with per-close and open-market caps lifted."""
    spec = next(s for s in DEFAULT_STRATEGIES if s.mode == "longshot")
    risk = spec.risk or RiskConfig()
    return replace(
        spec,
        longshot=replace(spec.longshot, max_event_cost=math.inf),
        risk=replace(risk, starting_cash=1e12, max_open_markets=1_000_000),
    )


def replay_episodes(
    directory: Path,
    start_ms: int | None = None,
    end_ms: int | None = None,
    spec: StrategySpec | None = None,
    tick_ms: int = 1000,
    progress: Callable[[int], None] | None = None,
) -> list[Episode]:
    """Run the long-shot strategy over a recording and return one Episode per market."""
    markets = load_markets(directory)
    state = LiveState()
    state.markets = {tk: m.info for tk, m in markets.items() if m.info.series in SERIES}
    spec = spec or research_spec()
    if start_ms is None or end_ms is None:
        first, last = _span(directory)
        start_ms = first if start_ms is None else start_ms
        end_ms = last + 1 if end_ms is None else end_ms
    clock = [start_ms]
    tape = _Tape()
    with tempfile.TemporaryDirectory() as tmp:
        engine = LiveEngine(
            state, Path(tmp) / "paper.json", clock=lambda: clock[0], strategies=[spec]
        )
        runner = engine.runners[0]
        book = runner.maker
        if book is None:
            raise ValueError(f"{spec.name} is not a maker strategy")
        acct = runner.account
        episodes: dict[str, Episode] = {}
        seen_orders: dict[str, int] = {}
        n_fills = 0
        next_tick = start_ms + tick_ms

        def on_tick(t: int) -> None:
            nonlocal n_fills
            for f in acct.fills[n_fills:]:
                ep = episodes.get(f.ticker)
                if f.action == "BUY" and ep is not None:
                    if ep.first_fill_ms is None:
                        ep.first_fill_ms, ep.fill_price = f.ts_ms, f.price
                    ep.filled_contracts += f.contracts
            n_fills = len(acct.fills)
            for tk, o in book.orders.items():
                ep = episodes.get(tk)
                if ep is None:
                    m = state.markets[tk]
                    ep = episodes[tk] = Episode(
                        tk,
                        tk.rsplit("-", 1)[0],
                        m.close_ts,
                        o.side,
                        o.placed_ms,
                        o.price,
                        o.queue_ahead,
                    )
                elif seen_orders.get(tk) != id(o):
                    ep.reposts += 1
                seen_orders[tk] = id(o)
                if ep.filled:
                    continue  # features only matter up to the first fill
                m = state.markets[tk]
                sign = _toward_sign(m.strike_type, ep.long_side)
                ep.samples.append(
                    _sample(ep, state, tape, t, o.price, o.queue_ahead, sign, m.floor)
                )

        def run_until(t: int) -> None:
            nonlocal next_tick
            while next_tick <= t:
                clock[0] = next_tick
                engine.tick()
                on_tick(next_tick)
                for tk in engine.pending_settlement:
                    res = markets.get(tk)
                    if res is not None and res.result and res.info.close_ts * 1000 <= next_tick:
                        engine.settle(tk, res.result)
                if progress is not None and next_tick % 3_600_000 < tick_ms:
                    progress(next_tick)
                next_tick += tick_ms

        for recv_ms, _, stream, r in events(directory, start_ms, end_ms, SERIES):
            run_until(recv_ms)
            if stream == "book":
                state.ws_last_ms = recv_ms
                _apply_book(state, r)
            elif stream == "trades":
                state.ws_last_ms = recv_ms
                count = float(r["count"])
                state.add_trade(r["ticker"], float(r["yes_price"]), count, r["taker_side"], recv_ms)
                tape.add_trade(r["ticker"], recv_ms, count, r["taker_side"])
            elif r["index_id"] == state.index_id:
                try:
                    value = float(json.loads(r["raw"])["value"])
                except (ValueError, KeyError, TypeError):
                    continue
                state.ws_last_ms = recv_ms
                state.index.append((recv_ms, value))
                tape.add_index(recv_ms, value)
                while state.index and state.index[0][0] < recv_ms - INDEX_KEEP_S * 1000:
                    state.index.popleft()
        run_until(end_ms)
    for ep in episodes.values():
        res = markets.get(ep.ticker)
        ep.result = res.result if res is not None else ""
    return sorted(episodes.values(), key=lambda e: (e.close_ts, e.ticker))


def _span(directory: Path) -> tuple[int, int]:
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    files = [str(f) for f in (directory / "trades").rglob("*.parquet") if f.stat().st_size > 0]
    t = ds.dataset(files).to_table(columns=["recv_ts_ms"])
    lo, hi = pc.min_max(t["recv_ts_ms"]).values()
    return int(lo.as_py()), int(hi.as_py())


# ------------------------------------------------------------------ analysis


def settled(episodes: Iterable[Episode]) -> list[Episode]:
    return [e for e in episodes if e.result in ("yes", "no")]


def split_closes(
    episodes: Sequence[Episode], share: float = EXPLORE_SHARE
) -> tuple[list[Episode], list[Episode]]:
    """Earliest ``share`` of closes (by close time) vs the rest."""
    closes = sorted({(e.close_ts, e.event) for e in episodes})
    cut = {ev for _, ev in closes[: int(len(closes) * share)]}
    return [e for e in episodes if e.event in cut], [e for e in episodes if e.event not in cut]


def trigger_ms(ep: Episode, rule: Rule) -> int | None:
    return next((s.t_ms for s in ep.samples if rule.fires(s)), None)


def avoided(ep: Episode, rule: Rule, latency_ms: int = CANCEL_LATENCY_MS) -> bool:
    """Would the rule have cancelled this market's order before its first fill?"""
    if ep.first_fill_ms is None:
        return False
    t = trigger_ms(ep, rule)
    return t is not None and t + latency_ms <= ep.first_fill_ms


def score_rule(episodes: Sequence[Episode], rule: Rule) -> dict[str, Any]:
    """Per-contract P&L of fills kept vs avoided, one entry per market, CIs by close."""
    fills = [e for e in episodes if e.filled]
    kept = [e for e in fills if not avoided(e, rule)]
    gone = [e for e in fills if avoided(e, rule)]
    cancelled_unfilled = sum(
        1 for e in episodes if not e.filled and trigger_ms(e, rule) is not None
    )
    return {
        "rule": asdict(rule),
        "all_fills": summary([e.firing() for e in fills]),
        "kept": summary([e.firing() for e in kept]),
        "avoided": summary([e.firing() for e in gone]),
        "kept_minus_all_cents": _diff_ci(fills, {id(e) for e in gone}),
        "unfilled_orders_cancelled": cancelled_unfilled,
        "unfilled_orders": sum(1 for e in episodes if not e.filled),
    }


def _diff_ci(
    fills: Sequence[Episode], gone: set[int], n_boot: int = 2000, seed: int = 0
) -> dict[str, float | None]:
    """Mean of kept fills minus mean of all fills (¢/contract), 95% CI resampling closes."""
    if not fills:
        return {"mean": None, "lo": None, "hi": None}
    evs = sorted({e.event for e in fills})
    idx = {ev: i for i, ev in enumerate(evs)}
    k = len(evs)
    s_all, n_all, s_kept, n_kept = (np.zeros(k) for _ in range(4))
    for e in fills:
        i = idx[e.event]
        s_all[i] += e.pnl
        n_all[i] += 1
        if id(e) not in gone:
            s_kept[i] += e.pnl
            n_kept[i] += 1

    def diff(w: np.ndarray) -> np.ndarray:
        kept = (s_kept * w).sum(axis=-1) / np.maximum((n_kept * w).sum(axis=-1), 1)
        return np.asarray(kept - (s_all * w).sum(axis=-1) / (n_all * w).sum(axis=-1))

    point = float(diff(np.ones(k)))
    rng = np.random.default_rng(seed)
    w = np.stack([np.bincount(rng.integers(0, k, k), minlength=k) for _ in range(n_boot)])
    lo, hi = np.percentile(diff(w.astype(float)), [2.5, 97.5])
    return {"mean": point * 100, "lo": float(lo) * 100, "hi": float(hi) * 100}


def choose_rule(explore: Sequence[Episode]) -> tuple[Rule | None, list[dict[str, Any]]]:
    """Best single-threshold rule on explore closes: highest mean P&L of kept fills among
    rules that keep at least MIN_KEPT_SHARE of fills. Thresholds are quantiles of each
    feature over all resting samples."""
    fills = [e for e in explore if e.filled]
    grid: list[dict[str, Any]] = []
    best: tuple[float, Rule] | None = None
    for feat in RULE_FEATURES:
        vals = np.array(
            [v for e in explore for s in e.samples if (v := getattr(s, feat)) is not None],
            dtype=float,
        )
        if len(vals) == 0:
            continue
        for q in RULE_QUANTILES:
            thr = float(np.quantile(vals, q))
            if thr <= 0:
                continue  # a rule that fires on "nothing happened" is not a signal
            rule = Rule(feat, thr)
            kept = [e for e in fills if not avoided(e, rule)]
            mean = float(np.mean([e.pnl for e in kept])) if kept else math.nan
            share = len(kept) / len(fills) if fills else 0.0
            grid.append(
                {
                    "feature": feat,
                    "quantile": q,
                    "threshold": thr,
                    "kept_share": share,
                    "kept_mean_cents": mean * 100,
                }
            )
            if share >= MIN_KEPT_SHARE and kept and (best is None or mean > best[0]):
                best = (mean, rule)
    return (best[1] if best else None), grid


def lead_table(episodes: Sequence[Episode]) -> dict[str, Any]:
    """Average features k seconds before the first fill, by outcome, vs unfilled orders."""
    groups: dict[str, list[Episode]] = {
        "filled_long_shot_won": [e for e in episodes if e.filled and e.won_long],
        "filled_long_shot_lost": [e for e in episodes if e.filled and not e.won_long],
    }
    feats = (
        "buy_10",
        "buy_30",
        "buy_60",
        "buy_300",
        "sell_60",
        "bid_up_30",
        "bid_up_60",
        "level",
        "level_drop_60",
        "queue",
        "toward_30",
        "toward_60",
        "toward_300",
        "dist_bps",
    )
    out: dict[str, Any] = {}
    for name, eps in groups.items():
        rows: dict[str, Any] = {"n": len(eps)}
        for k in LEAD_S:
            snaps = [
                s
                for e in eps
                if e.first_fill_ms is not None
                and (s := e.before(e.first_fill_ms - (k - 1) * 1000)) is not None
            ]
            rows[f"{k}s"] = _means(snaps, feats)
        out[name] = rows
    unfilled = [e for e in episodes if not e.filled and e.samples]
    # one value per order (its average while resting) so long-lived orders don't dominate
    per_order = [_means(e.samples, feats) for e in unfilled]
    out["unfilled_while_resting"] = {
        "n": len(unfilled),
        "avg": {f: _nanmean([p[f] for p in per_order]) for f in feats},
    }
    return out


def _means(samples: Sequence[Sample], feats: Sequence[str]) -> dict[str, float | None]:
    return {f: _nanmean([getattr(s, f) for s in samples]) for f in feats}


def _nanmean(vals: Iterable[float | None]) -> float | None:
    v = [x for x in vals if x is not None and not math.isnan(x)]
    return float(np.mean(v)) if v else None


def fill_overview(episodes: Sequence[Episode]) -> dict[str, Any]:
    fills = [e for e in episodes if e.filled]
    unfilled = [e for e in episodes if not e.filled]
    secs = [(e.first_fill_ms - e.placed_ms) / 1000 for e in fills if e.first_fill_ms is not None]
    return {
        "orders": summary([e.firing() for e in episodes]),
        "filled": summary([e.firing() for e in fills]),
        "unfilled": summary([e.firing() for e in unfilled]),
        "fill_rate": len(fills) / len(episodes) if episodes else None,
        "median_secs_to_fill": float(np.median(secs)) if secs else None,
        "median_queue_at_join": float(np.median([e.queue_at_join for e in episodes]))
        if episodes
        else None,
    }


# ------------------------------------------------------------------ paper comparison


def compare_with_paper(episodes: Sequence[Episode], paper: dict[str, Any]) -> dict[str, Any]:
    """Simulated vs paper-account fills on markets both saw (the paper account's first BUY
    per market). Only markets that closed while both were running are compared."""
    started = int(paper.get("started_ms") or 0)
    first: dict[str, dict[str, Any]] = {}
    for f in paper.get("fills", []):
        if f.get("action") == "BUY" and f["ticker"] not in first:
            first[f["ticker"]] = f
    sim_lo = min((e.placed_ms for e in episodes), default=0)
    sim_hi = max((e.close_ts * 1000 for e in episodes), default=0)
    sim = {e.ticker: e for e in episodes if e.placed_ms >= started}
    paper_in = {tk: f for tk, f in first.items() if sim_lo <= f["ts_ms"] <= sim_hi}
    both = sorted(set(sim) & set(paper_in))
    sim_filled = {tk for tk, e in sim.items() if e.filled}
    lag = [(sim[tk].first_fill_ms - paper_in[tk]["ts_ms"]) / 1000 for tk in both if sim[tk].filled]
    return {
        "sim_markets": len(sim),
        "paper_filled_markets": len(paper_in),
        "both_filled": len([tk for tk in both if tk in sim_filled]),
        "paper_only": len(set(paper_in) - sim_filled),
        "sim_only": len(sim_filled - set(paper_in)),
        "median_fill_lag_s": float(np.median(lag)) if lag else None,
        "price_matches": sum(
            1
            for tk in both
            if sim[tk].fill_price is not None
            and abs(sim[tk].fill_price - paper_in[tk]["price"]) < 1e-9
        ),
    }


# ------------------------------------------------------------------ report


def analyse(episodes: Sequence[Episode], rule: Rule | None = None) -> dict[str, Any]:
    eps = settled(episodes)
    explore, test = split_closes(eps)
    chosen, grid = choose_rule(explore)
    frozen = rule or PREREGISTERED_RULE or chosen
    closes = sorted({e.close_ts for e in eps})
    return {
        "data": {
            "episodes": len(eps),
            "closes": len(closes),
            "first_close": datetime.fromtimestamp(closes[0], UTC).isoformat() if closes else None,
            "last_close": datetime.fromtimestamp(closes[-1], UTC).isoformat() if closes else None,
            "explore_closes": len({e.event for e in explore}),
            "test_closes": len({e.event for e in test}),
            "unsettled_skipped": len(episodes) - len(eps),
        },
        "fills_all": fill_overview(eps),
        "fills_explore": fill_overview(explore),
        "fills_test": fill_overview(test),
        "lead_explore": lead_table(explore),
        "lead_test": lead_table(test),
        "rule_grid_explore": grid,
        "rule_chosen_on_explore": asdict(chosen) if chosen else None,
        "rule_evaluated": asdict(frozen) if frozen else None,
        "rule_explore": score_rule(explore, frozen) if frozen else None,
        "rule_test": score_rule(test, frozen) if frozen else None,
    }


def save_episodes(episodes: Sequence[Episode], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(e) for e in episodes]))


def load_episodes(path: Path) -> list[Episode]:
    out = []
    for d in json.loads(path.read_text()):
        samples = [Sample(**s) for s in d.pop("samples")]
        out.append(Episode(**d, samples=samples))
    return out


def main() -> None:  # pragma: no cover - research entry point
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=Path("data/recorder"))
    parser.add_argument(
        "--episodes", type=Path, default=Path("reports/longshot-fill-episodes.json")
    )
    parser.add_argument("--out", type=Path, default=Path("reports/longshot-adverse-fills.json"))
    parser.add_argument("--paper", type=Path, default=None, help="paper-longshot.json to compare")
    parser.add_argument("--reuse", action="store_true", help="skip the replay, reuse --episodes")
    args = parser.parse_args()
    if args.reuse:
        eps = load_episodes(args.episodes)
    else:

        def progress(t: int) -> None:
            print(datetime.fromtimestamp(t / 1000, UTC).isoformat(), flush=True)

        eps = replay_episodes(args.data, progress=progress)
        save_episodes(eps, args.episodes)
    result = analyse(eps)
    if args.paper is not None:
        result["paper_comparison"] = compare_with_paper(eps, json.loads(args.paper.read_text()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "rule_grid_explore"}, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
