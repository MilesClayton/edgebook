"""One plain-English call per market, recomputed from live data.

Entry calls: BUY UP, BUY DOWN, or NO TRADE (with the first reason that blocked it).
Position calls: HOLD or SELL, by comparing what selling pays *now* (best bid
minus fee) with what the position is expected to be worth at settlement (the
model probability). Nothing here places orders.

"UP" means YES on an up/down or "above $K" market; "DOWN" means NO.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.engine.state import Level, LiveState, MarketInfo
from edgebook.model.pricer import prob_settle_above

WINDOW_S = 60


@dataclass(frozen=True)
class EngineConfig:
    min_edge: float = 0.04  # $ per contract after the taker fee
    max_spread: float = 0.05
    max_index_age_ms: int = 3_000
    max_ws_silence_ms: int = 10_000
    max_spot_index_gap_bps: float = 15.0  # exchanges ahead of the index -> wait
    min_secs_left: float = 45.0
    max_secs_left: float = 3600.0  # short-horizon vol model: don't price hours-away markets
    min_history_s: float = 900.0  # need 15 min of index ticks before trusting volatility
    sell_hysteresis: float = 0.005
    nu: float = 4.0  # Student-t tails: best-calibrated variant in our tests
    min_sigma_per_s: float = 3.5e-5  # ~0.027%/min floor: quiet-market BTC, never calmer


@dataclass(frozen=True)
class Position:
    side: str  # "yes" | "no"
    contracts: float
    avg_cost: float  # $ per contract incl. entry fee


@dataclass
class Call:
    ticker: str
    series: str
    action: str  # BUY UP | BUY DOWN | NO TRADE | HOLD | SELL
    reason: str
    seconds_left: float
    strike: float | None
    index: float | None
    index_age_ms: int | None
    fair_up: float | None = None
    up_price: float | None = None  # cost to buy UP now (best YES ask)
    down_price: float | None = None  # cost to buy DOWN now (1 - best YES bid)
    up_sell: float | None = None  # what selling UP pays now (best YES bid)
    down_sell: float | None = None
    edge_up: float | None = None  # fair - price - fee, $ per contract
    edge_down: float | None = None
    spread: float | None = None
    depth_up: float = 0.0  # contracts available at the best UP price
    depth_down: float = 0.0
    position: Position | None = None
    sigma_per_min_bps: float | None = None
    market_up: float | None = None  # Kalshi's implied P(UP): mid of best bid/ask
    buy_up_below: float | None = None  # highest UP price that still clears min_edge
    buy_down_below: float | None = None
    sell_up_at: float | None = None  # lowest UP bid at which selling beats holding
    sell_down_at: float | None = None
    z_score: float | None = None  # index distance from strike in remaining std devs
    gates: dict[str, bool] = field(default_factory=dict)


def fee(price: float, role: LiquidityRole = LiquidityRole.TAKER) -> float:
    p = min(max(price, 0.0), 1.0)
    return float(trade_fee(1, Decimal(f"{p:.4f}"), role))


def buy_limit(fair: float, min_edge: float) -> float | None:
    """Highest whole-cent price p with fair - p - fee(p) >= min_edge (None if none)."""
    best = None
    for cents in range(1, 100):
        p = cents / 100
        if fair - p - fee(p) >= min_edge - 1e-9:
            best = p
    return best


def sell_trigger(fair: float, hysteresis: float) -> float | None:
    """Lowest whole-cent bid b where b - fee(b) >= fair - hysteresis (None if never)."""
    for cents in range(1, 100):
        b = cents / 100
        if b - fee(b) >= fair - hysteresis - 1e-9:
            return b
    return None


def fair_up(state: LiveState, m: MarketInfo, now_ms: int, cfg: EngineConfig) -> float | None:
    """P(UP wins) for this market from the live index (and its known partial average)."""
    cur = state.index_now()
    sig = state.sigma_per_s(now_ms)
    if cur is None or sig is None:
        return None
    sig = max(sig, cfg.min_sigma_per_s)
    left = m.close_ts - now_ms / 1000
    if left <= 0:
        return None
    strike = m.floor if m.floor is not None else m.cap
    if strike is None:
        return None
    known = None
    if left < WINDOW_S:
        avg = state.index_avg_since((m.close_ts - WINDOW_S) * 1000)
        known = avg[0] if avg else cur[1]
    p_above = prob_settle_above(cur[1], strike, left, sig * sig, known_avg=known, nu=cfg.nu)
    if m.strike_type in ("less", "less_or_equal"):
        return 1 - p_above
    return p_above


def _best(levels: list[Level]) -> Level | None:
    return levels[0] if levels else None


def evaluate(
    state: LiveState,
    m: MarketInfo,
    now_ms: int,
    cfg: EngineConfig,
    position: Position | None = None,
) -> Call:
    left = m.close_ts - now_ms / 1000
    cur = state.index_now()
    idx_age = now_ms - cur[0] if cur else None
    call = Call(
        ticker=m.ticker,
        series=m.series,
        action="NO TRADE",
        reason="",
        seconds_left=left,
        strike=m.floor if m.floor is not None else m.cap,
        index=cur[1] if cur else None,
        index_age_ms=idx_age,
        position=position,
    )
    sig = state.sigma_per_s(now_ms)
    call.sigma_per_min_bps = sig * math.sqrt(60) * 1e4 if sig else None

    up_ask, down_ask = _best(state.asks(m.ticker, "yes")), _best(state.asks(m.ticker, "no"))
    up_bid, down_bid = _best(state.bids(m.ticker, "yes")), _best(state.bids(m.ticker, "no"))
    call.up_price = up_ask.price if up_ask else None
    call.down_price = down_ask.price if down_ask else None
    call.up_sell = up_bid.price if up_bid else None
    call.down_sell = down_bid.price if down_bid else None
    call.depth_up = up_ask.size if up_ask else 0.0
    call.depth_down = down_ask.size if down_ask else 0.0
    if up_ask and up_bid:
        call.spread = round(up_ask.price - up_bid.price, 4)

    p = fair_up(state, m, now_ms, cfg)
    call.fair_up = p
    if up_ask and up_bid:
        call.market_up = (up_ask.price + up_bid.price) / 2
    sig_now = state.sigma_per_s(now_ms)
    if cur and call.strike and sig_now and left > 0:
        horizon = max(left - WINDOW_S, 0) + min(left, WINDOW_S) / 3
        call.z_score = math.log(cur[1] / call.strike) / (
            max(sig_now, cfg.min_sigma_per_s) * math.sqrt(horizon)
        )
        if m.strike_type in ("less", "less_or_equal"):
            call.z_score = -call.z_score
    if p is not None:
        call.buy_up_below = buy_limit(p, cfg.min_edge)
        call.buy_down_below = buy_limit(1 - p, cfg.min_edge)
        call.sell_up_at = sell_trigger(p, cfg.sell_hysteresis)
        call.sell_down_at = sell_trigger(1 - p, cfg.sell_hysteresis)
        if up_ask:
            call.edge_up = p - up_ask.price - fee(up_ask.price)
        if down_ask:
            call.edge_down = (1 - p) - down_ask.price - fee(down_ask.price)

    # ---- data-quality gates (the "don't buy on the lag" checks) -------------
    gap_bps = None
    if state.composite is not None and state.composite.mid and cur:
        gap_bps = abs(state.composite.mid / cur[1] - 1) * 1e4
    gates = {
        "market open": left > 0,
        "settlement index live (< 3 s old)": idx_age is not None
        and idx_age <= cfg.max_index_age_ms,
        "Kalshi stream live": now_ms - state.ws_last_ms <= cfg.max_ws_silence_ms,
        "volatility estimate ready": sig is not None,
        "15 min of index history (warm-up)": state.history_s(now_ms) >= cfg.min_history_s,
        "within 60-min model horizon": left <= cfg.max_secs_left,
        "strike known": call.strike is not None,
        "exchanges in line with index": gap_bps is None or gap_bps <= cfg.max_spot_index_gap_bps,
        "both sides quoted": up_ask is not None and down_ask is not None,
        "spread tight enough": call.spread is not None and call.spread <= cfg.max_spread,
    }
    call.gates = gates

    # ---- open position: HOLD or SELL ---------------------------------------
    if position is not None:
        if p is None:
            call.action, call.reason = (
                "HOLD",
                "No live fair value right now; holding to settlement.",
            )
            return call
        p_side = p if position.side == "yes" else 1 - p
        bid = up_bid if position.side == "yes" else down_bid
        name = "UP" if position.side == "yes" else "DOWN"
        if bid is None:
            call.action = "HOLD"
            call.reason = (
                f"No buyers for {name} right now; expected value {p_side:.2f} at settlement."
            )
            return call
        sell_now = bid.price - fee(bid.price)
        if sell_now >= p_side - cfg.sell_hysteresis:
            call.action = "SELL"
            call.reason = (
                f"Selling {name} pays {bid.price:.2f} now ({sell_now:.3f} after fee), at or above "
                f"its fair value {p_side:.3f}: take the money."
            )
        else:
            call.action = "HOLD"
            call.reason = (
                f"{name} is worth about {p_side:.3f} at settlement but only {sell_now:.3f} "
                f"if sold now: hold."
            )
        return call

    # ---- no position: BUY UP / BUY DOWN / NO TRADE -------------------------
    failed = [name for name, ok in gates.items() if not ok]
    if failed:
        call.reason = f"Waiting: {failed[0]}."
        return call
    if left < cfg.min_secs_left:
        call.reason = f"Too close to close ({left:.0f}s left) to enter safely."
        return call
    if p is None or call.edge_up is None or call.edge_down is None:
        call.reason = "Waiting: no live fair value yet."
        return call
    best_side, best_edge = (
        ("UP", call.edge_up) if call.edge_up >= call.edge_down else ("DOWN", call.edge_down)
    )
    if best_edge >= cfg.min_edge:
        call.action = f"BUY {best_side}"
        price = call.up_price if best_side == "UP" else call.down_price
        fair = p if best_side == "UP" else 1 - p
        call.reason = (
            f"Model gives {best_side} a {fair:.0%} chance; it costs {price:.2f}. "
            f"Expected profit {best_edge * 100:.1f}¢ per contract after fees."
        )
    else:
        call.reason = (
            f"Price is fair: best edge {best_edge * 100:+.1f}¢ ({best_side}) is below the "
            f"{cfg.min_edge * 100:.0f}¢ minimum."
        )
    return call
