"""Price open Kalshi BTC markets right now and compute after-fee edges.

Spot is Coinbase's latest 1-minute close (freshly backfilled), volatility is a
HAR fit on stored history, and quotes come from Kalshi's public market list.
The same ``Signal`` rows feed the dashboard and ``trade-once``, so what you see
is exactly what would be traded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.history import coinbase
from edgebook.model.pricer import contract_prob, prob_settle_above
from edgebook.model.vol import HOUR_S, HarModel, hourly_realized
from edgebook.venues.kalshi.models import RestMarket
from edgebook.venues.kalshi.rest import KalshiRest

NU = 4.0  # fat tails: the best-calibrated variant in the 30-day calibration


@dataclass(frozen=True)
class Signal:
    ticker: str
    series: str
    strike_type: str
    floor: float | None
    cap: float | None
    close_time: datetime
    seconds_left: float
    spot: float
    fair: float
    bid: float
    ask: float
    edge_yes: float  # fair - ask - taker fee, per contract
    edge_no: float  # (1 - fair) - (1 - bid) - taker fee, per contract

    @property
    def best_side(self) -> str:
        return "yes" if self.edge_yes >= self.edge_no else "no"

    @property
    def best_edge(self) -> float:
        return max(self.edge_yes, self.edge_no)


def _fee(price: float) -> float:
    p = min(max(round(price, 4), 0.0), 1.0)
    return float(trade_fee(1, Decimal(str(p)), LiquidityRole.TAKER))


class LivePricer:
    def __init__(self, history_dir: Path) -> None:
        self.history_dir = history_dir
        self._model: HarModel | None = None
        self._fit_hour: int | None = None

    async def refresh_spot(self) -> tuple[int, float]:
        """Top up stored 1m history to now; return (ts, close) of the latest minute."""
        await coinbase.backfill(self.history_dir, days=10)
        ts, close = coinbase.load(self.history_dir)
        day = int(ts[-1]) // HOUR_S * HOUR_S // 86_400
        self._hourly = hourly_realized(ts, close)
        if self._model is None or self._fit_hour != day:
            self._model = HarModel.fit(self._hourly)  # refit once per UTC day
            self._fit_hour = day
        return int(ts[-1]), float(close[-1])

    def price(self, m: RestMarket, series: str, spot: float, now: datetime) -> Signal | None:
        if self._model is None:
            raise RuntimeError("call refresh_spot() first")
        if m.yes_bid_dollars is None or m.yes_ask_dollars is None:
            return None
        bid, ask = float(m.yes_bid_dollars), float(m.yes_ask_dollars)
        if ask <= 0 or ask <= bid:
            return None
        seconds = max((m.close_time - now).total_seconds(), 0.0)
        if seconds < 60:
            return None  # inside the averaging window; needs the live BRTI average
        start = int(now.timestamp())
        var = self._model.variance_between(self._hourly, start, start + int(seconds)) / seconds
        floor = float(m.floor_strike) if m.floor_strike is not None else None
        cap = float(m.cap_strike) if m.cap_strike is not None else None
        p_floor = None if floor is None else prob_settle_above(spot, floor, seconds, var, nu=NU)
        p_cap = None if cap is None else prob_settle_above(spot, cap, seconds, var, nu=NU)
        fair = min(max(contract_prob(m.strike_type or "", floor, cap, p_floor, p_cap), 0.0), 1.0)
        return Signal(
            ticker=m.ticker,
            series=series,
            strike_type=m.strike_type or "",
            floor=floor,
            cap=cap,
            close_time=m.close_time,
            seconds_left=seconds,
            spot=spot,
            fair=fair,
            bid=bid,
            ask=ask,
            edge_yes=fair - ask - _fee(ask),
            edge_no=(1 - fair) - (1 - bid) - _fee(1 - bid),
        )


async def signals(
    rest: KalshiRest, pricer: LivePricer, series: str, max_markets: int = 40
) -> list[Signal]:
    """Signals for the soonest-closing open event in ``series``, nearest strikes first."""
    _, spot = await pricer.refresh_spot()
    now = datetime.now(UTC)
    markets = await rest.markets(series, status="open")
    if not markets:
        return []
    soonest = min(m.close_time for m in markets)
    out = [
        s
        for m in markets
        if m.close_time == soonest
        for s in [pricer.price(m, series, spot, now)]
        if s is not None
    ]
    out.sort(key=lambda s: abs(s.fair - 0.5))
    return out[:max_markets]
