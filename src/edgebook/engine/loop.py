"""The live loop: every second, re-evaluate every live market for each strategy and
let that strategy's paper account act on its calls; publish calls + accounts to
the dashboard store.

Several strategies run side by side on the same live data, each with its own
$500k paper account, so their results can be compared directly. One of them
("primary") drives the Live Calls page.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from edgebook.engine.decide import Call, EngineConfig, evaluate, fee
from edgebook.engine.maker import MakerBook, MakerConfig
from edgebook.engine.paper import PaperAccount, RiskConfig
from edgebook.engine.state import LiveState, MarketInfo
from edgebook.model.calibration import mean_ci

log = logging.getLogger(__name__)

TRADED_SERIES = ("KXBTC15M", "KXBTCD")
HOURLY_STRIKES_SHOWN = 6
DECISION_LOG_EVERY_MS = 10_000
FITTED_WEIGHT_FILE = "model_weight.json"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class StrategySpec:
    name: str
    label: str
    description: str
    cfg: EngineConfig = field(default_factory=EngineConfig)
    mode: str = "taker"  # "taker": buys at the ask | "maker": rests bids and waits
    maker: MakerConfig = field(default_factory=MakerConfig)
    uses_fitted_weight: bool = False  # take model_weight from model_weight.json when present


DEFAULT_STRATEGIES: tuple[StrategySpec, ...] = (
    StrategySpec(
        "v1",
        "v1 taker (control)",
        "The original rules: trust the model fully, buy at the ask when it sees 4¢+ edge, "
        "sell as soon as the bid reaches fair value.",
        EngineConfig(),
    ),
    StrategySpec(
        "anchored",
        "Anchored taker",
        "Fair value starts at Kalshi's price and the model only nudges it (20% weight). "
        "Buys at the ask on 4¢+ edge, holds to the result, sells only to take 3¢+ profit.",
        EngineConfig(model_weight=0.2, exit_mode="take_profit", take_profit=0.03),
        uses_fitted_weight=True,
    ),
    StrategySpec(
        "anchored50",
        "Anchored taker (50% model)",
        "Like the anchored taker but the model gets half the weight, so it trades more often. "
        "Tests whether the model adds anything once it is anchored to Kalshi's price.",
        EngineConfig(model_weight=0.5, exit_mode="take_profit", take_profit=0.03),
    ),
    StrategySpec(
        "maker",
        "Maker (resting bids)",
        "Same anchored fair value, but never pays the ask: rests a bid 2¢+ below fair value "
        "(maker fee is a quarter of the taker fee), fills only when real trades hit it, "
        "holds to the result.",
        EngineConfig(model_weight=0.2, exit_mode="take_profit", take_profit=1.0),
        mode="maker",
        uses_fitted_weight=True,
    ),
)
PRIMARY = "anchored"


class StrategyRunner:
    """One strategy's config, paper account and call history."""

    def __init__(self, spec: StrategySpec, account_path: Path, risk: RiskConfig | None) -> None:
        self.spec = spec
        self.cfg = spec.cfg
        self.account_path = account_path
        self.account = PaperAccount.load(account_path, risk)
        # risk limits come from code, not from whatever an older account file saved
        self.account.risk = risk or RiskConfig()
        self.calls: list[Call] = []
        self.pending_settlement: set[str] = set()
        self.last_action: dict[str, str] = {}
        self.signal_log: deque[dict[str, Any]] = deque(maxlen=200)
        self.maker = MakerBook() if spec.mode == "maker" else None

    def save(self) -> None:
        self.account.save(self.account_path)


def account_path_for(base: Path, spec: StrategySpec, first: bool) -> Path:
    """The first strategy keeps the original file (its history continues)."""
    return base if first else base.with_name(f"{base.stem}-{spec.name}{base.suffix}")


def load_fit(directory: Path) -> dict[str, Any] | None:
    """The saved model-weight fit (``edgebook strategy-report --write-weight``), if valid."""
    try:
        fit = json.loads((directory / FITTED_WEIGHT_FILE).read_text())
        w = float(fit["model_weight"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not math.isfinite(w):
        return None
    fit["model_weight"] = min(max(w, 0.0), 1.0)
    return dict(fit)


class LiveEngine:
    def __init__(
        self,
        state: LiveState,
        account_path: Path,
        cfg: EngineConfig | None = None,
        risk: RiskConfig | None = None,
        clock: Callable[[], int] = now_ms,
        strategies: Sequence[StrategySpec] | None = None,
        primary: str | None = None,
        decision_log_dir: Path | None = None,
    ) -> None:
        self.state = state
        self.clock = clock
        specs = (
            list(strategies)
            if strategies
            else [StrategySpec("v1", "v1", "", cfg or EngineConfig())]
        )
        self.fit = load_fit(account_path.parent)
        fitted = self.fit["model_weight"] if self.fit else None
        if fitted is not None:
            specs = [
                replace(s, cfg=replace(s.cfg, model_weight=fitted)) if s.uses_fitted_weight else s
                for s in specs
            ]
            log.info("using fitted model weight %.3f", fitted)
        self.runners = [
            StrategyRunner(s, account_path_for(account_path, s, i == 0), risk)
            for i, s in enumerate(specs)
        ]
        names = [r.spec.name for r in self.runners]
        self.primary = next(
            (r for r in self.runners if r.spec.name == (primary or PRIMARY)), self.runners[0]
        )
        if len(set(names)) != len(names):
            raise ValueError(f"strategy names must be unique: {names}")
        self.decision_log_dir = decision_log_dir
        self._last_decision_ms = 0

    # ---- primary-strategy shortcuts (Live Calls page, tests) -----------------
    @property
    def cfg(self) -> EngineConfig:
        return self.primary.cfg

    @property
    def account(self) -> PaperAccount:
        return self.primary.account

    @property
    def calls(self) -> list[Call]:
        return self.primary.calls

    @property
    def signal_log(self) -> deque[dict[str, Any]]:
        return self.primary.signal_log

    @property
    def pending_settlement(self) -> set[str]:
        return set().union(*(r.pending_settlement for r in self.runners))

    # ---- market selection ---------------------------------------------------
    def live_markets(self, t_ms: int, runner: StrategyRunner | None = None) -> list[MarketInfo]:
        """Open 15-min markets, plus the hourly strikes nearest the index for the
        next hourly close (and any still-open market the account holds)."""
        runner = runner or self.primary
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
            for t in runner.account.holdings
            if t not in held and t in self.state.markets
        ]
        return out

    # ---- one tick -----------------------------------------------------------
    def tick(self) -> list[Call]:
        t = self.clock()
        for r in self.runners:
            self._tick_runner(r, t)
        if (
            self.decision_log_dir is not None
            and t - self._last_decision_ms >= DECISION_LOG_EVERY_MS
        ):
            self._last_decision_ms = t
            self._log_decisions(t)
        return self.primary.calls

    def _tick_runner(self, r: StrategyRunner, t: int) -> None:
        acct = r.account
        if r.maker is not None:
            r.maker.match(self.state, acct)
        marks = self._marks(r)
        equity = acct.equity(marks)
        acct.roll_day(datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m-%d"), equity)
        halted = acct.halted(equity)
        calls = []
        for m in self.live_markets(t, r):
            call = evaluate(self.state, m, t, r.cfg, acct.position(m.ticker))
            if call.action == "SELL":
                acct.sell(m.ticker, self.state.bids(m.ticker, call.position.side), t, call.reason)  # type: ignore[union-attr]
            elif r.maker is not None:
                self._maker_call(r, call, equity, t, halted)
            elif call.action.startswith("BUY") and not halted:
                side = "yes" if call.action == "BUY UP" else "no"
                fair = call.fair_up if side == "yes" else 1 - (call.fair_up or 0)
                # never pay more than fair - fee - min_edge for any contract
                max_price = (fair or 0) - r.cfg.min_edge - fee(0.5)
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
            self._log_change(r, call, t)
            calls.append(call)
        if r.maker is not None:  # markets no longer evaluated keep no orders
            for tk in set(r.maker.orders) - {c.ticker for c in calls}:
                r.maker.cancel(tk)
        # Held markets that are past close, or no longer listed (e.g. closed before a
        # restart), wait for Kalshi's result.
        r.pending_settlement = {
            tk
            for tk in acct.holdings
            if tk not in self.state.markets or self.state.markets[tk].close_ts <= t / 1000
        }
        if not acct.equity_series or t - acct.equity_series[-1][0] >= 10_000:
            acct.equity_series.append((t, round(acct.equity(self._marks(r)), 2)))
        r.calls = calls

    def _maker_call(
        self, r: StrategyRunner, call: Call, equity: float, t: int, halted: bool
    ) -> None:
        if r.maker is None:
            return
        gates_ok = all(call.gates.values()) and call.seconds_left >= r.cfg.min_secs_left
        order = r.maker.quote(
            self.state, r.account, call, r.spec.maker, equity, t, gates_ok and not halted
        )
        if call.position is not None and order is None:
            return  # HOLD / SELL call from evaluate stands
        if order is None:
            if call.action.startswith("BUY") or not call.reason.startswith("Waiting"):
                call.action = "NO TRADE"
                call.reason = (
                    "Daily loss limit reached: entries paused until tomorrow (UTC)."
                    if halted
                    else call.reason
                    if not gates_ok
                    else f"No bid {r.spec.maker.edge * 100:.0f}¢+ below fair value fits "
                    "inside the spread."
                )
            return
        name = "UP" if order.side == "yes" else "DOWN"
        call.action = f"BID {name}"
        call.reason = (
            f"Resting a {name} bid at {order.price:.2f} for {order.contracts:,.0f} contracts "
            f"(fair value {order.fair:.2f}); fills only if a seller hits it."
        )

    def _log_change(self, r: StrategyRunner, call: Call, t: int) -> None:
        """Record every time a market's call changes (the 'when to act' history)."""
        prev = r.last_action.get(call.ticker)
        if prev == call.action:
            return
        r.last_action[call.ticker] = call.action
        if prev is None and call.action == "NO TRADE":
            return  # don't log the first quiet state of every market
        r.signal_log.append(
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

    def _log_decisions(self, t: int) -> None:
        """Append one row per evaluated market (all of them, traded or not) so the
        model-vs-market weight can be refit without selection bias."""
        if self.decision_log_dir is None:
            return
        rows = [
            {
                "ts_ms": t,
                "ticker": c.ticker,
                "series": c.series,
                "strike": c.strike,
                "seconds_left": round(c.seconds_left, 1),
                "index": c.index,
                "model_up": c.model_up,
                "market_up": c.market_up,
                "up_ask": c.up_price,
                "up_bid": c.up_sell,
                "spread": c.spread,
                "sigma_per_min_bps": c.sigma_per_min_bps,
                "gates_ok": all(c.gates.values()),
            }
            for c in self.primary.calls
            if c.model_up is not None
        ]
        if not rows:
            return
        day = datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m-%d")
        try:
            self.decision_log_dir.mkdir(parents=True, exist_ok=True)
            with (self.decision_log_dir / f"{day}.jsonl").open("a") as fh:
                fh.writelines(json.dumps(row) + "\n" for row in rows)
        except OSError:
            log.exception("decision log write failed")

    def settle(self, ticker: str, result: str) -> None:
        for r in self.runners:
            if r.account.settle(ticker, result, self.clock()) is not None:
                r.pending_settlement.discard(ticker)
                r.save()

    def save(self) -> None:
        for r in self.runners:
            r.save()

    def _marks(self, r: StrategyRunner | None = None) -> dict[str, float]:
        out = {}
        for tk, h in (r or self.primary).account.holdings.items():
            bids = self.state.bids(tk, h.side)
            if bids:
                out[tk] = bids[0].price - fee(bids[0].price)
        return out

    # ---- dashboard payloads ------------------------------------------------
    def live_payload(self) -> dict[str, Any]:
        t = self.clock()
        cur = self.state.index_now()
        comp = self.state.composite
        maker = next((r for r in self.runners if r.maker is not None), None)
        return {
            "updated_ms": t,
            "strategy": self.primary.spec.name,
            "strategy_label": self.primary.spec.label,
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
            "maker_orders": [asdict(o) for o in maker.maker.orders.values()]
            if maker is not None and maker.maker is not None
            else [],
        }

    def paper_payload(self, name: str | None = None) -> dict[str, Any]:
        r = next((x for x in self.runners if x.spec.name == name), self.primary)
        acct = r.account
        marks = self._marks(r)
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
                    "awaiting_result": tk in r.pending_settlement,
                }
            )
        closed = acct.closed
        wins = [pnl for pnl, _ in closed if pnl > 0]
        per_contract = mean_ci(np.array([pnl / n for pnl, n in closed if n > 0], dtype=float))
        contracts = sum(n for _, n in closed)
        return {
            "updated_ms": self.clock(),
            "name": r.spec.name,
            "label": r.spec.label,
            "description": r.spec.description,
            "mode": r.spec.mode,
            "model_weight": r.cfg.model_weight,
            "primary": r is self.primary,
            "starting_cash": acct.risk.starting_cash,
            "cash": acct.cash,
            "equity": equity,
            "realized": acct.realized,
            "unrealized": sum(h["unrealized"] or 0.0 for h in holdings),
            "return_pct": (equity / acct.risk.starting_cash - 1) * 100,
            "halted": acct.halted(equity),
            "closed_trades": len(closed),
            "win_rate": len(wins) / len(closed) if closed else None,
            "per_contract": asdict(per_contract),
            "pnl_per_contract": acct.realized / contracts if contracts else None,
            "holdings": holdings,
            "resting": [asdict(o) for o in r.maker.orders.values()] if r.maker else [],
            "fills": [asdict(f) for f in acct.fills[-300:]],
            "equity_series": acct.equity_series[-3000:],
            "risk": asdict(acct.risk),
            "started_ms": acct.started_ms,
        }

    def strategies_payload(self) -> dict[str, Any]:
        """Every strategy's account, lighter than ``paper`` (fewer fills, thinned equity)."""
        accounts = []
        for r in self.runners:
            p = self.paper_payload(r.spec.name)
            eq = p["equity_series"]
            step = max(1, len(eq) // 600)
            p["equity_series"] = eq[::step] + ([eq[-1]] if eq and (len(eq) - 1) % step else [])
            p["fills"] = p["fills"][-150:]
            accounts.append(p)
        return {
            "updated_ms": self.clock(),
            "primary": self.primary.spec.name,
            "fit": self.fit,
            "accounts": accounts,
        }
