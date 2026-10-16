"""Tick strategies for the replay engine.

``LagStrategy`` is CloddsBot's divergence idea adapted to Kalshi: when BRTI
moves at least ``min_move_pct`` over a short window and the contract's mid has
not followed, buy the side the move favors, either crossing the spread (taker)
or joining the best bid on our side (maker), and hold to settlement (one fee)
or exit on a time stop. KXBTC15M strikes are the open average; KXBTCD strikes
are fixed, so both directions map onto YES/NO through strike type.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

from edgebook.core.types import LiquidityRole
from edgebook.model.pricer import prob_settle_above
from edgebook.research.tick import Context, Fill, fee


@dataclass
class LagStrategy:
    window_s: float = 15.0
    min_move_pct: float = 0.08
    max_mid_follow: float = 0.02
    maker: bool = False
    hold_s: float | None = None  # None = hold to settlement
    min_secs_left: float = 90.0  # stay out of the averaging window
    max_entry: float = 0.85
    series_prefix: str = "KXBTC15M"
    cooldown_s: float = 60.0
    name: str = "lag"
    _mid_hist: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    _last_entry: dict[str, int] = field(default_factory=dict)
    _open: list[Fill] = field(default_factory=list)

    def on_book(self, ctx: Context, ticker: str) -> None:
        if not ticker.startswith(self.series_prefix):
            return
        bid, _, ask, _ = ctx.best(ticker)
        if bid is not None and ask is not None and ask > bid:
            hist = self._mid_hist.setdefault(ticker, [])
            hist.append((ctx.now_ms, (bid + ask) / 2))
            if len(hist) > 5000:
                del hist[:2500]

    def _mid_ago(self, ticker: str, now_ms: int, secs: float) -> float | None:
        target = now_ms - secs * 1000
        prev = None
        for ts, mid in self._mid_hist.get(ticker, []):
            if ts > target:
                break
            prev = mid
        return prev

    def on_index(self, ctx: Context) -> None:
        if self.hold_s is not None:
            for f in list(self._open):
                if ctx.now_ms - f.ts_ms >= self.hold_s * 1000 and ctx.sell(f, self.name):
                    self._open.remove(f)
        now_v, then_v = ctx.index_value, ctx.index_at(self.window_s)
        if now_v is None or then_v is None:
            return
        move = (now_v / then_v - 1) * 100
        if abs(move) < self.min_move_pct:
            return
        for tk, m in ctx.markets.items():
            if not tk.startswith(self.series_prefix):
                continue
            secs_left = m.close_ts - ctx.now_ms / 1000
            if secs_left < self.min_secs_left or secs_left > 3600:
                continue
            if ctx.now_ms - self._last_entry.get(tk, 0) < self.cooldown_s * 1000:
                continue
            bid, _, ask, _ = ctx.best(tk)
            then_mid = self._mid_ago(tk, ctx.now_ms, self.window_s)
            if bid is None or ask is None or then_mid is None:
                continue
            up_is_yes = m.strike_type not in ("less", "less_or_equal")
            side = "yes" if (move > 0) == up_is_yes else "no"
            mid = (bid + ask) / 2
            followed = (mid - then_mid) if side == "yes" else (then_mid - mid)
            price = ask if side == "yes" else 1 - bid
            if followed >= self.max_mid_follow or price > self.max_entry:
                continue
            self._last_entry[tk] = ctx.now_ms
            if self.maker:
                join = bid if side == "yes" else 1 - ask
                ctx.rest(tk, side, round(join, 2), ttl_s=self.window_s, tag=self.name)
            else:
                got = ctx.take(tk, side, self.name)
                if got is not None and self.hold_s is not None:
                    self._open.append(got)


def _sigma_per_s(ctx: Context, lookback_s: float = 900.0) -> float | None:
    """Per-second log-vol of BRTI from 1 Hz index ticks over the lookback."""
    cutoff = ctx.now_ms - lookback_s * 1000
    vals = [v for ts, v in ctx.index_hist[-int(lookback_s) - 5 :] if ts >= cutoff]
    if len(vals) < 60:
        return None
    r = [math.log(b / a) for a, b in itertools.pairwise(vals) if a > 0]
    if len(r) < 30:
        return None
    mean = sum(r) / len(r)
    var = sum((x - mean) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(var)


@dataclass
class SettlementWindow:
    """Price the 60s settlement average exactly once part of it is already printed.

    Inside the final 60 s, settlement = (printed part + remaining part) / 60, so
    only the remainder is random. Take the mispriced side when model - ask >= edge.
    """

    min_edge: float = 0.03
    min_left_s: float = 5.0
    max_left_s: float = 55.0
    series_prefix: str = "KXBTC"
    name: str = "settle_window"
    _entered: set[str] = field(default_factory=set)

    def on_book(self, ctx: Context, ticker: str) -> None:
        return None

    def on_index(self, ctx: Context) -> None:
        sig = _sigma_per_s(ctx)
        if sig is None or ctx.index_value is None:
            return
        for tk, m in ctx.markets.items():
            if not tk.startswith(self.series_prefix) or tk in self._entered:
                continue
            left = m.close_ts - ctx.now_ms / 1000
            if not self.min_left_s <= left <= self.max_left_s:
                continue
            start_ms = (m.close_ts - 60) * 1000
            printed = [v for ts, v in ctx.index_hist[-70:] if ts >= start_ms]
            if not printed or m.strike_type not in ("greater", "greater_or_equal"):
                continue
            strike = m.floor
            if strike is None:
                continue
            known = sum(printed) / len(printed)
            p_yes = prob_settle_above(
                ctx.index_value, strike, left, sig * sig, known_avg=known, nu=4.0
            )
            bid, _, ask, _ = ctx.best(tk)
            if bid is None or ask is None:
                continue
            for side, p, price in (("yes", p_yes, ask), ("no", 1 - p_yes, 1 - bid)):
                if (
                    0.02 < price < 0.98
                    and p - price - fee(price, LiquidityRole.TAKER) >= self.min_edge
                ):
                    if ctx.take(tk, side, self.name) is not None:
                        self._entered.add(tk)
                    break


@dataclass
class PairedBids:
    """Rest YES and NO bids at the touch when together they cost <= max_pair."""

    max_pair: float = 0.97
    refresh_s: float = 5.0
    start_after_s: float = 20.0
    stop_before_s: float = 48.0
    max_imbalance: int = 3
    series_prefix: str = "KXBTC15M"
    name: str = "paired_bids"
    _last: dict[str, int] = field(default_factory=dict)

    def on_index(self, ctx: Context) -> None:
        return None

    def on_book(self, ctx: Context, ticker: str) -> None:
        if not ticker.startswith(self.series_prefix):
            return
        m = ctx.markets.get(ticker)
        if m is None or ctx.now_ms - self._last.get(ticker, 0) < self.refresh_s * 1000:
            return
        left = m.close_ts - ctx.now_ms / 1000
        if left < self.stop_before_s or left > 900 - self.start_after_s:
            return
        bid, _, ask, _ = ctx.best(ticker)
        if bid is None or ask is None:
            return
        no_bid = 1 - ask
        if bid + no_bid > self.max_pair:
            return
        self._last[ticker] = ctx.now_ms
        held = ctx.position[ticker]
        yes_n = sum(1 for f in held if f.side == "yes")
        no_n = len(held) - yes_n
        if yes_n - no_n < self.max_imbalance:
            ctx.keep_or_rest(ticker, "yes", round(bid, 4), 3 * self.refresh_s, self.name)
        if no_n - yes_n < self.max_imbalance:
            ctx.keep_or_rest(ticker, "no", round(no_bid, 4), 3 * self.refresh_s, self.name)


@dataclass
class AvellanedaStoikov:
    """Inventory-skewed two-sided quotes (Sidi3355 parameters), spread capped at 4c."""

    gamma: float = 0.1
    max_spread: float = 0.04
    refresh_s: float = 5.0
    max_inventory: int = 20
    stop_before_s: float = 90.0
    series_prefix: str = "KXBTC15M"
    name: str = "avellaneda_stoikov"
    _last: dict[str, int] = field(default_factory=dict)

    def on_index(self, ctx: Context) -> None:
        return None

    def on_book(self, ctx: Context, ticker: str) -> None:
        if not ticker.startswith(self.series_prefix):
            return
        m = ctx.markets.get(ticker)
        if m is None or ctx.now_ms - self._last.get(ticker, 0) < self.refresh_s * 1000:
            return
        left = m.close_ts - ctx.now_ms / 1000
        if left < self.stop_before_s or left > 900:
            return
        bid, _, ask, _ = ctx.best(ticker)
        if bid is None or ask is None or ask <= bid:
            return
        self._last[ticker] = ctx.now_ms
        held = ctx.position[ticker]
        q = sum(1 if f.side == "yes" else -1 for f in held)
        mid = (bid + ask) / 2
        sigma = 0.01  # ~1c per minute mid volatility; refined once we have more data
        t_frac = left / 900
        reservation = mid - q * self.gamma * sigma**2 * t_frac * 100
        half = min(self.max_spread, max(ask - bid, 0.01)) / 2
        yes_px = round(min(reservation - half, ask - 0.01), 2)
        no_px = round(min(1 - (reservation + half), 1 - bid - 0.01), 2)
        if q < self.max_inventory and 0 < yes_px < 1:
            ctx.keep_or_rest(ticker, "yes", yes_px, 3 * self.refresh_s, self.name)
        if q > -self.max_inventory and 0 < no_px < 1:
            ctx.keep_or_rest(ticker, "no", no_px, 3 * self.refresh_s, self.name)
