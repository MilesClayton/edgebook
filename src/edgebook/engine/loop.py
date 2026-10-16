"""The live loop: every second, re-evaluate every live market and let the paper
account act on the calls; publish calls + account to the dashboard store."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from edgebook.engine.decide import Call, EngineConfig, evaluate, fee
from edgebook.engine.paper import PaperAccount, RiskConfig
from edgebook.engine.state import LiveState, MarketInfo

log = logging.getLogger(__name__)

TRADED_SERIES = ("KXBTC15M", "KXBTCD")
HOURLY_STRIKES_SHOWN = 6


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class LiveEngine:
    def __init__(
        self,
        state: LiveState,
        account_path: Path,
        cfg: EngineConfig | None = None,
        risk: RiskConfig | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.state = state
        self.cfg = cfg or EngineConfig()
        self.account_path = account_path
        self.account = PaperAccount.load(account_path, risk)
        self.clock = clock
        self.calls: list[Call] = []
        self.pending_settlement: set[str] = set()
        self.last_action: dict[str, str] = {}
        self.signal_log: deque[dict[str, Any]] = deque(maxlen=200)

    # ---- market selection ---------------------------------------------------
    def live_markets(self, t_ms: int) -> list[MarketInfo]:
        """Open 15-min markets, plus the hourly strikes nearest the index for the
        next hourly close (and any market we hold)."""
        now_s = t_ms / 1000
        live = [m for m in self.state.markets.values() if m.open_ts <= now_s < m.close_ts]
        out = [m for m in live if m.series == "KXBTC15M"]
        hourly = [m for m in live if m.series == "KXBTCD" and m.floor is not None]
        cur = self.state.index_now()
        if hourly and cur:
            nxt = min(m.close_ts for m in hourly)
            nearest = sorted(
                (m for m in hourly if m.close_ts == nxt), key=lambda m: abs((m.floor or 0) - cur[1])
            )
            out += nearest[:HOURLY_STRIKES_SHOWN]
        held = {m.ticker for m in out}
        out += [
            self.state.markets[t]
            for t in self.account.holdings
            if t not in held and t in self.state.markets
        ]
        return out

    # ---- one tick -----------------------------------------------------------
    def tick(self) -> list[Call]:
        t = self.clock()
        acct = self.account
        marks = self._marks()
        equity = acct.equity(marks)
        acct.roll_day(datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m-%d"), equity)
        halted = acct.halted(equity)
        calls = []
        for m in self.live_markets(t):
            call = evaluate(self.state, m, t, self.cfg, acct.position(m.ticker))
            if call.action == "SELL":
                acct.sell(m.ticker, self.state.bids(m.ticker, call.position.side), t, call.reason)  # type: ignore[union-attr]
            elif call.action.startswith("BUY") and not halted:
                side = "yes" if call.action == "BUY UP" else "no"
                fair = call.fair_up if side == "yes" else 1 - (call.fair_up or 0)
                # never pay more than fair - fee - min_edge for any contract
                max_price = (fair or 0) - self.cfg.min_edge - fee(0.5)
                acct.buy(
                    m.ticker,
                    side,
                    self.state.asks(m.ticker, side),
                    max_price,
                    equity,
                    t,
                    call.reason,
                )
            elif call.action.startswith("BUY") and halted:
                call.action, call.reason = (
                    "NO TRADE",
                    "Daily loss limit reached: entries paused until tomorrow (UTC).",
                )
            self._log_change(call, t)
            calls.append(call)
        # markets past close that we still hold wait for Kalshi's result
        self.pending_settlement = {
            tk
            for tk in acct.holdings
            if tk in self.state.markets and self.state.markets[tk].close_ts <= t / 1000
        }
        if not acct.equity_series or t - acct.equity_series[-1][0] >= 10_000:
            acct.equity_series.append((t, round(acct.equity(self._marks()), 2)))
        self.calls = calls
        return calls

    def _log_change(self, call: Call, t: int) -> None:
        """Record every time a market's call changes (the 'when to act' history)."""
        prev = self.last_action.get(call.ticker)
        if prev == call.action:
            return
        self.last_action[call.ticker] = call.action
        if prev is None and call.action == "NO TRADE":
            return  # don't log the first quiet state of every market
        self.signal_log.append(
            {
                "ts_ms": t,
                "ticker": call.ticker,
                "series": call.series,
                "strike": call.strike,
                "from": prev,
                "to": call.action,
                "fair_up": call.fair_up,
                "up_price": call.up_price,
                "down_price": call.down_price,
                "reason": call.reason,
            }
        )

    def settle(self, ticker: str, result: str) -> None:
        if self.account.settle(ticker, result, self.clock()) is not None:
            self.save()

    def save(self) -> None:
        self.account.save(self.account_path)

    def _marks(self) -> dict[str, float]:
        out = {}
        for tk, h in self.account.holdings.items():
            bids = self.state.bids(tk, h.side)
            if bids:
                out[tk] = bids[0].price - fee(bids[0].price)
        return out

    # ---- dashboard payloads ------------------------------------------------
    def live_payload(self) -> dict[str, Any]:
        t = self.clock()
        cur = self.state.index_now()
        comp = self.state.composite
        return {
            "updated_ms": t,
            "index": cur[1] if cur else None,
            "index_age_ms": t - cur[0] if cur else None,
            "ws_age_ms": t - self.state.ws_last_ms if self.state.ws_last_ms else None,
            "exchanges_mid": comp.mid if comp else None,
            "exchanges_age_ms": t - self.state.composite_ms if comp else None,
            "exchanges": {k: asdict(v) for k, v in comp.venues.items()} if comp else {},
            "index_series": [[ts, v] for ts, v in list(self.state.index)[-900:]],
            "history_s": self.state.history_s(t),
            "min_history_s": self.cfg.min_history_s,
            "config": asdict(self.cfg),
            "calls": [asdict(c) for c in self.calls],
            "signal_log": list(self.signal_log)[-100:],
        }

    def paper_payload(self) -> dict[str, Any]:
        acct = self.account
        marks = self._marks()
        equity = acct.equity(marks)
        holdings = []
        for tk, h in acct.holdings.items():
            mark = marks.get(tk)
            holdings.append(
                {
                    "ticker": tk,
                    "side": "UP" if h.side == "yes" else "DOWN",
                    "contracts": h.contracts,
                    "avg_cost": h.avg_cost,
                    "cost": h.cost,
                    "mark": mark,
                    "unrealized": None if mark is None else h.contracts * mark - h.cost,
                    "awaiting_result": tk in self.pending_settlement,
                }
            )
        closed = [f for f in acct.fills if f.pnl is not None]
        wins = [f for f in closed if (f.pnl or 0) > 0]
        return {
            "updated_ms": self.clock(),
            "starting_cash": acct.risk.starting_cash,
            "cash": acct.cash,
            "equity": equity,
            "realized": acct.realized,
            "unrealized": sum(h["unrealized"] or 0.0 for h in holdings),
            "return_pct": (equity / acct.risk.starting_cash - 1) * 100,
            "halted": acct.halted(equity),
            "closed_trades": len(closed),
            "win_rate": len(wins) / len(closed) if closed else None,
            "holdings": holdings,
            "fills": [asdict(f) for f in acct.fills[-300:]],
            "equity_series": acct.equity_series[-3000:],
            "risk": asdict(acct.risk),
            "started_ms": acct.started_ms,
        }
