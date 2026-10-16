"""Paper account that trades the engine's calls against real order-book depth.

Fills walk the live book level by level (taker), so a $500k account cannot
pretend to buy more than is actually offered. Every fill pays Kalshi's taker
fee. Positions settle at $1/$0 on Kalshi's published result. No orders are
ever sent anywhere.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from edgebook.engine.decide import Position, fee
from edgebook.engine.state import Level


@dataclass(frozen=True)
class RiskConfig:
    starting_cash: float = 500_000.0
    max_risk_per_trade: float = 0.01  # fraction of equity spent per entry
    max_per_market: float = 25_000.0  # $ cost basis cap per market
    max_open_markets: int = 6
    daily_loss_limit: float = 0.05  # fraction of start-of-day equity
    cooldown_s: float = 30.0


@dataclass
class Fill:
    ts_ms: int
    ticker: str
    action: str  # BUY | SELL | SETTLE
    side: str
    contracts: float
    price: float  # avg $ per contract (before fees)
    fees: float
    cash_after: float
    reason: str
    pnl: float | None = None  # realized on SELL / SETTLE


@dataclass
class Holding:
    side: str
    contracts: float
    cost: float  # total $ paid incl. fees
    opened_ms: int

    @property
    def avg_cost(self) -> float:
        return self.cost / self.contracts if self.contracts else 0.0


@dataclass
class PaperAccount:
    risk: RiskConfig = field(default_factory=RiskConfig)
    cash: float = 0.0
    holdings: dict[str, Holding] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    equity_series: list[tuple[int, float]] = field(default_factory=list)
    realized: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    last_entry_ms: dict[str, int] = field(default_factory=dict)
    started_ms: int = 0

    def __post_init__(self) -> None:
        if not self.started_ms:
            self.cash = self.risk.starting_cash
            self.day_start_equity = self.risk.starting_cash
            self.started_ms = int(time.time() * 1000)

    # ---- queries -----------------------------------------------------------
    def position(self, ticker: str) -> Position | None:
        h = self.holdings.get(ticker)
        return Position(h.side, h.contracts, h.avg_cost) if h else None

    def equity(self, marks: dict[str, float]) -> float:
        """Cash + holdings marked at what selling would pay now (bid, after fee)."""
        total = self.cash
        for tk, h in self.holdings.items():
            total += h.contracts * marks.get(tk, h.avg_cost)
        return total

    def halted(self, equity: float) -> bool:
        return equity < self.day_start_equity * (1 - self.risk.daily_loss_limit)

    # ---- trading -----------------------------------------------------------
    def roll_day(self, day: str, equity: float) -> None:
        if day != self.day:
            self.day, self.day_start_equity = day, equity

    def buy(
        self,
        ticker: str,
        side: str,
        asks: list[Level],
        max_price: float,
        equity: float,
        now_ms: int,
        reason: str,
    ) -> Fill | None:
        """Walk asks up to ``max_price`` within the per-trade and per-market budgets."""
        if ticker in self.holdings:
            return None  # one position per market, never add to it
        if len(self.holdings) >= self.risk.max_open_markets:
            return None
        if now_ms - self.last_entry_ms.get(ticker, 0) < self.risk.cooldown_s * 1000:
            return None
        budget = min(equity * self.risk.max_risk_per_trade, self.risk.max_per_market, self.cash)
        spent = qty = fees = 0.0
        for lvl in asks:
            if lvl.price > max_price or budget - spent <= 0:
                break
            unit = lvl.price + fee(lvl.price)
            take = min(lvl.size, (budget - spent) // unit)
            if take <= 0:
                break
            spent += take * unit
            fees += take * fee(lvl.price)
            qty += take
        if qty <= 0:
            return None
        self.cash -= spent
        self.holdings[ticker] = Holding(side, qty, spent, now_ms)
        self.last_entry_ms[ticker] = now_ms
        f = Fill(now_ms, ticker, "BUY", side, qty, (spent - fees) / qty, fees, self.cash, reason)
        self.fills.append(f)
        return f

    def sell(self, ticker: str, bids: list[Level], now_ms: int, reason: str) -> Fill | None:
        h = self.holdings.get(ticker)
        if h is None:
            return None
        left = h.contracts
        proceeds = fees = sold = 0.0
        for lvl in bids:
            take = min(lvl.size, left - sold)
            if take <= 0:
                break
            proceeds += take * (lvl.price - fee(lvl.price))
            fees += take * fee(lvl.price)
            sold += take
        if sold <= 0:
            return None
        cost_part = h.cost * sold / h.contracts
        pnl = proceeds - cost_part
        self.cash += proceeds
        self.realized += pnl
        if sold >= h.contracts:
            del self.holdings[ticker]
        else:
            h.contracts -= sold
            h.cost -= cost_part
        f = Fill(
            now_ms,
            ticker,
            "SELL",
            h.side,
            sold,
            (proceeds + fees) / sold,
            fees,
            self.cash,
            reason,
            pnl,
        )
        self.fills.append(f)
        return f

    def settle(self, ticker: str, result: str, now_ms: int) -> Fill | None:
        h = self.holdings.pop(ticker, None)
        if h is None:
            return None
        won = (result == "yes") == (h.side == "yes")
        payout = h.contracts * (1.0 if won else 0.0)
        pnl = payout - h.cost
        self.cash += payout
        self.realized += pnl
        f = Fill(
            now_ms,
            ticker,
            "SETTLE",
            h.side,
            h.contracts,
            1.0 if won else 0.0,
            0.0,
            self.cash,
            f"Market settled {result.upper()}",
            pnl,
        )
        self.fills.append(f)
        return f

    # ---- persistence -------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "risk": asdict(self.risk),
            "cash": self.cash,
            "holdings": {k: asdict(v) for k, v in self.holdings.items()},
            "fills": [asdict(f) for f in self.fills[-2000:]],
            "equity_series": self.equity_series[-5000:],
            "realized": self.realized,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "last_entry_ms": self.last_entry_ms,
            "started_ms": self.started_ms,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> PaperAccount:
        acct = cls(
            risk=RiskConfig(**d["risk"]),
            cash=d["cash"],
            realized=d["realized"],
            day=d["day"],
            day_start_equity=d["day_start_equity"],
            last_entry_ms=dict(d["last_entry_ms"]),
            started_ms=d["started_ms"],
        )
        acct.holdings = {k: Holding(**v) for k, v in d["holdings"].items()}
        acct.fills = [Fill(**f) for f in d["fills"]]
        acct.equity_series = [(int(t), float(e)) for t, e in d["equity_series"]]
        return acct

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json()))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path, risk: RiskConfig | None = None) -> PaperAccount:
        if path.is_file():
            return cls.from_json(json.loads(path.read_text()))
        return cls(risk=risk or RiskConfig())
