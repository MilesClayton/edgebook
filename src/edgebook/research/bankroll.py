"""Monte Carlo bankroll simulator: what edge does "safely double the budget" need?

The owner's goal is not big profits but to *safely* turn the saved trading budget into
twice that. This module makes "safely" a testable bar (``Bar``) and checks any strategy
against it by simulating many bankroll paths of Kalshi binary trades:

- **Outcome distribution.** Each trade buys one side of a $1 binary at a price, wins with
  a true probability, and pays the Kalshi fee from ``engine.decide.fee`` (taker or maker,
  rounded up to the cent per contract). ``TradeMix`` mixes several such payoffs, and
  ``TradeMix.at_edge`` builds one with a chosen true edge per contract *after* fees.
- **Dependence.** All strikes of one hourly close move together, so trades are grouped
  into closes. A Gaussian copula with latent correlation ``rho`` links the trades of one
  close: ``rho=1`` makes them comonotone (the worst case, a ladder of strikes on one side),
  ``rho=0`` makes them independent. Each trade keeps its own win probability either way.
- **Sizing.** Fixed contracts, a fixed fraction of current wealth risked per close, or a
  fraction of the Kelly stake (split across the close's trades, since they are correlated).
- **Caps.** The live ledger's money limits (``execution.live_ledger.Limits``): capital
  budget, per-order debit, open exposure, realized loss, max contracts, and the pilot's
  per-close limit. Reservations use the ledger's worst-case fee reserve, like the ledger.

Two accounting modes matter a great deal:

- ``"ledger"`` is the ledger as built: realized loss is the *gross* sum of losing trades,
  and profits never refund it (``docs/LIVE_EXECUTION.md``, "Budget semantics"). Once gross
  losses reach the budget the account stops for good. Doubling then needs a true edge per
  contract larger than the expected gross loss per contract, which no strategy here has.
- ``"net"`` counts loss as the drawdown of wealth below the starting budget, and lets the
  stake grow with wealth. This is what "compound the budget to 2x" means.

Run ``uv run python -m edgebook.research.bankroll`` for the tables in
``docs/BANKROLL_BAR.md`` (writes ``reports/bankroll-bar.json``; a few minutes).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray
from scipy.stats import norm

from edgebook.core.types import LiquidityRole
from edgebook.engine.decide import fee
from edgebook.execution.live_ledger import DOLLAR, Limits, fee_reserve

FloatArray = NDArray[np.float64]

# The ledger reserves this worst-case fee per contract while an order is open (2.76c).
FEE_RESERVE = fee_reserve(100) / DOLLAR
_EPS = 1e-9

# Long-shot seller: buy the favourite at 85-98c (the long shot is offered at 2-15c).
LONGSHOT_PRICES = (0.86, 0.90, 0.94, 0.97)
# Near-50/50 taker: cross the spread on an at-the-money strike.
COINFLIP_PRICES = (0.50,)


@dataclass(frozen=True)
class Payoff:
    """Buy one $1 binary contract at ``price``; it pays $1 with probability ``win_prob``."""

    price: float
    win_prob: float
    role: LiquidityRole = LiquidityRole.MAKER

    def __post_init__(self) -> None:
        if not 0 < self.price < 1:
            raise ValueError("price must be strictly between 0 and 1")
        if not 0 <= self.win_prob <= 1:
            raise ValueError("win_prob must be in [0, 1]")

    @property
    def fee(self) -> float:
        return fee(self.price, self.role)

    @property
    def cost(self) -> float:
        """What a losing contract loses: price plus fee."""
        return self.price + self.fee

    @property
    def edge(self) -> float:
        """True expected profit per contract after fees."""
        return self.win_prob - self.cost

    @property
    def reserve(self) -> float:
        """Worst-case debit the ledger reserves per contract."""
        return self.price + FEE_RESERVE

    @classmethod
    def with_edge(cls, price: float, edge: float, role: LiquidityRole) -> Payoff:
        """The payoff at ``price`` with true ``edge`` after fees; the edge can be at most
        ``1 - cost`` (a sure win), e.g. 2c at 97c."""
        return cls(price, price + fee(price, role) + edge, role)


@dataclass(frozen=True)
class TradeMix:
    """Each trade draws one payoff with the given weights."""

    payoffs: tuple[Payoff, ...]
    weights: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.payoffs or len(self.payoffs) != len(self.weights):
            raise ValueError("need one weight per payoff")
        if min(self.weights) < 0 or not math.isclose(sum(self.weights), 1.0):
            raise ValueError("weights must be non-negative and sum to 1")

    @classmethod
    def at_edge(
        cls,
        prices: Sequence[float],
        edge: float,
        role: LiquidityRole,
        weights: Sequence[float] | None = None,
    ) -> TradeMix:
        """A mix whose mean true edge per contract after fees is ``edge``.

        The edge is spread in proportion to each contract's upside ``1 - cost``: a 97c
        favourite cannot carry the same 3c edge as an 86c one without becoming a sure win
        (which Kelly would bet everything on). Every contract then has the same Kelly
        fraction. ``edge`` can be at most the mean upside, where every contract is sure."""
        w = tuple(weights) if weights is not None else (1 / len(prices),) * len(prices)
        upside = [1 - p - fee(p, role) for p in prices]
        mean_upside = float(np.dot(w, upside))
        if edge > mean_upside + _EPS:
            raise ValueError(f"edge above the mix's mean upside ({mean_upside:.4f})")
        return cls(
            tuple(
                Payoff.with_edge(p, edge * u / mean_upside, role)
                for p, u in zip(prices, upside, strict=True)
            ),
            w,
        )

    def _mean(self, values: Sequence[float]) -> float:
        return float(np.dot(self.weights, values))

    @property
    def edge(self) -> float:
        return self._mean([p.edge for p in self.payoffs])

    @property
    def win_prob(self) -> float:
        return self._mean([p.win_prob for p in self.payoffs])

    @property
    def price(self) -> float:
        return self._mean([p.price for p in self.payoffs])

    @property
    def gross_loss_rate(self) -> float:
        """Expected gross loss per contract: what the ledger's loss counter grows by."""
        return self._mean([(1 - p.win_prob) * p.cost for p in self.payoffs])

    @property
    def sd(self) -> float:
        """Standard deviation of one contract's profit (outcome and price draw)."""
        second = self._mean(
            [p.win_prob * (1 - p.cost) ** 2 + (1 - p.win_prob) * p.cost**2 for p in self.payoffs]
        )
        return math.sqrt(max(second - self.edge**2, 0.0))


def longshot_seller(edge: float) -> TradeMix:
    """Long-shot-seller-like payoff: maker buys of the favourite at 86-97c."""
    return TradeMix.at_edge(LONGSHOT_PRICES, edge, LiquidityRole.MAKER)


def coinflip_taker(edge: float) -> TradeMix:
    """Near-50/50 taker payoff: buy at 50c and pay the taker fee (2c)."""
    return TradeMix.at_edge(COINFLIP_PRICES, edge, LiquidityRole.TAKER)


@dataclass(frozen=True)
class Sizing:
    """How many contracts each trade buys.

    - ``contracts``: a fixed number per trade.
    - ``fraction``: risk ``value`` of current wealth per close, split over its trades.
    - ``kelly``: stake ``value`` times the Kelly fraction ``(p - cost) / (1 - cost)`` of
      current wealth per close, split over its trades. It sizes on the *true* edge, so it
      is an upper bound on what an estimated edge could safely support.
    """

    kind: Literal["contracts", "fraction", "kelly"]
    value: float

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise ValueError("sizing value must be positive")

    @property
    def label(self) -> str:
        if self.kind == "contracts":
            return f"{self.value:g} contracts"
        if self.kind == "fraction":
            return f"{self.value:.0%} of wealth per close"
        return f"{self.value:g}x Kelly"

    def contracts(
        self, wealth: FloatArray, win: FloatArray, cost: FloatArray, per_close: int
    ) -> FloatArray:
        if self.kind == "contracts":
            return np.full_like(wealth, self.value)
        if self.kind == "fraction":
            stake = self.value * wealth / per_close
        else:
            kelly = np.clip((win - cost) / (1 - cost), 0.0, 1.0)
            stake = self.value * kelly * wealth / per_close
        return np.floor(np.maximum(stake, 0.0) / cost + _EPS)


@dataclass(frozen=True)
class Caps:
    """Live money caps in dollars (``None`` = no cap). See ``Limits`` and ``check_entry``."""

    budget: float
    order_debit: float | None = None
    open_exposure: float | None = None
    realized_loss: float | None = None
    per_close: float | None = None
    max_contracts: int | None = None
    accounting: Literal["ledger", "net"] = "ledger"

    def __post_init__(self) -> None:
        if self.budget <= 0:
            raise ValueError("budget must be positive")

    @classmethod
    def from_limits(
        cls,
        limits: Limits,
        per_close_cents: int | None = None,
        accounting: Literal["ledger", "net"] = "ledger",
    ) -> Caps:
        """The ledger's capital-budget mode (what the dashboard's Save budget sets)."""
        if limits.capital_budget_cents is None:
            raise ValueError("only the capital-budget mode is modelled")
        return cls(
            budget=limits.capital_budget_cents / 100,
            order_debit=limits.order_debit_cents / 100,
            open_exposure=limits.open_exposure_cents / 100,
            realized_loss=limits.realized_loss_cents / 100,
            per_close=None if per_close_cents is None else per_close_cents / 100,
            max_contracts=limits.max_contracts,
            accounting=accounting,
        )

    @classmethod
    def saved_budget(cls, dollars: float, accounting: Literal["ledger", "net"] = "ledger") -> Caps:
        """Mirror ``ExecutionService.set_budget``: every cap equals the budget."""
        cents = round(dollars * 100)
        return cls.from_limits(
            Limits(
                capital_budget_cents=cents,
                purchase_spend_cents=cents,
                open_exposure_cents=cents,
                realized_loss_cents=cents,
                order_debit_cents=cents,
                max_contracts=10000,
            ),
            accounting=accounting,
        )


@dataclass(frozen=True)
class Bar:
    """What "safely double the budget" means. Defaults: within 90 days, reach 2x with at
    least 50% probability, and lose half the budget first with at most 5% probability."""

    target_multiple: float = 2.0
    horizon_days: int = 90
    min_p_target: float = 0.50
    ruin_fraction: float = 0.50
    max_p_ruin: float = 0.05

    def __post_init__(self) -> None:
        if self.target_multiple <= 1 or not 0 < self.ruin_fraction <= 1:
            raise ValueError("target must exceed 1x and ruin fraction must be in (0, 1]")


@dataclass(frozen=True)
class Scenario:
    mix: TradeMix
    sizing: Sizing
    caps: Caps
    closes_per_day: int = 24
    trades_per_close: int = 1
    rho: float = 1.0

    def __post_init__(self) -> None:
        if self.closes_per_day < 1 or self.trades_per_close < 1:
            raise ValueError("need at least one close per day and one trade per close")
        if not 0 <= self.rho <= 1:
            raise ValueError("rho must be in [0, 1]")

    @property
    def trades_per_day(self) -> int:
        return self.closes_per_day * self.trades_per_close


@dataclass(frozen=True)
class SimResult:
    paths: int
    p_target: float  # reached the target within the horizon, before ruin
    p_ruin: float  # wealth fell to (1 - ruin_fraction) x budget before the target
    p_stalled: float  # a loss or budget cap left no room for one contract, before either
    median_days_to_target: float | None
    median_final_multiple: float
    contracts_per_day: float
    passes: bool


def _stops(wealth: FloatArray, start: float, bar: Bar) -> tuple[NDArray[np.bool_], ...]:
    return wealth >= bar.target_multiple * start - _EPS, wealth <= (1 - bar.ruin_fraction) * start


def simulate(scenario: Scenario, bar: Bar, paths: int = 2000, seed: int = 0) -> SimResult:
    """Simulate ``paths`` bankrolls for ``bar.horizon_days``; target and ruin absorb."""
    mix, caps, m = scenario.mix, scenario.caps, scenario.trades_per_close
    rng = np.random.default_rng(seed)
    cost = np.array([p.cost for p in mix.payoffs])
    reserve = np.array([p.reserve for p in mix.payoffs])
    win = np.array([p.win_prob for p in mix.payoffs])
    threshold = np.asarray(
        norm.ppf(win), dtype=float
    )  # latent < threshold <=> win, so P(win) is kept exactly
    weights = np.array(mix.weights)
    start = caps.budget
    loss_limit = min(caps.realized_loss or math.inf, start if caps.accounting == "ledger" else 1e18)
    fixed_room = min(caps.open_exposure or math.inf, caps.per_close or math.inf)
    min_reserve = float(reserve.min())
    a, b = math.sqrt(scenario.rho), math.sqrt(1 - scenario.rho)

    wealth = np.full(paths, start)
    gross_loss = np.zeros(paths)
    done = np.zeros(paths, dtype=bool)
    hit, ruined, stalled = (np.zeros(paths, dtype=bool) for _ in range(3))
    days_to_target = np.full(paths, np.nan)
    contracts = 0.0
    for day in range(bar.horizon_days):
        for _ in range(scenario.closes_per_day):
            loss_used = gross_loss if caps.accounting == "ledger" else np.maximum(start - wealth, 0)
            room = np.minimum(np.minimum(wealth, loss_limit - loss_used), fixed_room)
            stuck = ~done & (room < min_reserve - _EPS)
            stalled |= stuck
            done |= stuck
            if done.all():
                break
            shock = rng.standard_normal(paths)
            pnl = np.zeros(paths)
            for _ in range(m):
                k = rng.choice(len(weights), size=paths, p=weights)
                n = scenario.sizing.contracts(wealth, win[k], cost[k], m)
                if caps.max_contracts is not None:
                    n = np.minimum(n, caps.max_contracts)
                if caps.order_debit is not None:
                    n = np.minimum(n, np.floor(caps.order_debit / reserve[k] + _EPS))
                n = np.where(
                    done, 0.0, np.clip(np.minimum(n, np.floor(room / reserve[k] + _EPS)), 0, None)
                )
                room = room - n * reserve[k]
                won = a * shock + b * rng.standard_normal(paths) < threshold[k]
                pnl += n * (won - cost[k])
                gross_loss += np.where(won, 0.0, n * cost[k])
                contracts += float(n.sum())
            wealth = wealth + pnl
            up, down = _stops(wealth, start, bar)
            new_hit, new_ruin = ~done & up, ~done & down & ~up
            days_to_target[new_hit] = day + 1
            hit |= new_hit
            ruined |= new_ruin
            done |= new_hit | new_ruin
        if done.all():
            break
    reached = days_to_target[hit]
    p_target, p_ruin = float(hit.mean()), float(ruined.mean())
    return SimResult(
        paths=paths,
        p_target=p_target,
        p_ruin=p_ruin,
        p_stalled=float((stalled & ~hit & ~ruined).mean()),
        median_days_to_target=float(np.median(reached)) if reached.size else None,
        median_final_multiple=float(np.median(wealth) / start),
        contracts_per_day=contracts / paths / bar.horizon_days,
        passes=p_target >= bar.min_p_target and p_ruin <= bar.max_p_ruin,
    )


def required_edge(
    make_mix: Callable[[float], TradeMix],
    sizing: Sizing,
    caps: Caps,
    bar: Bar,
    *,
    closes_per_day: int = 24,
    trades_per_close: int = 1,
    rho: float = 1.0,
    hi: float = 0.10,
    step: float = 0.0025,
    paths: int = 1000,
    seed: int = 0,
) -> float | None:
    """Smallest true mean edge per contract (dollars, after fees) on a ``step`` grid up to
    ``hi`` that passes ``bar``, with common random numbers; ``None`` if none does.

    A grid scan, not bisection: with Kelly sizing a bigger edge also means bigger bets, so
    passing is not guaranteed to be monotone in the edge. Edges the mix cannot carry end
    the scan."""
    for i in range(1, round(hi / step) + 1):
        try:
            mix = make_mix(i * step)
        except ValueError:
            break
        scenario = Scenario(mix, sizing, caps, closes_per_day, trades_per_close, rho)
        if simulate(scenario, bar, paths, seed).passes:
            return mix.edge
    return None


def close_sd(
    mix: TradeMix, trades_per_close: int, rho: float, sims: int = 200_000, seed: int = 0
) -> float:
    """SD across closes of the average profit per contract in one close (one contract per
    trade), with the same copula as ``simulate``."""
    rng = np.random.default_rng(seed)
    cost = np.array([p.cost for p in mix.payoffs])
    threshold = np.asarray(norm.ppf([p.win_prob for p in mix.payoffs]), dtype=float)
    shock = rng.standard_normal((sims, 1))
    k = rng.choice(len(mix.weights), size=(sims, trades_per_close), p=np.array(mix.weights))
    latent = math.sqrt(rho) * shock + math.sqrt(1 - rho) * rng.standard_normal(k.shape)
    pnl = (latent < threshold[k]) - cost[k]
    return float(pnl.mean(axis=1).std(ddof=1))


def closes_needed(
    mix: TradeMix,
    edge: float,
    trades_per_close: int = 1,
    rho: float = 1.0,
    alpha: float = 0.05,
    power: float = 0.8,
    sd: float | None = None,
) -> int:
    """Independent closes needed before a true ``edge`` per contract is distinguishable
    from zero: a two-sided ``alpha`` test (resampling closes) rejects zero with ``power``.
    ``power=0.5`` is "the 95% interval just excludes zero at the true edge"."""
    if edge <= 0:
        raise ValueError("edge must be positive")
    spread = close_sd(mix, trades_per_close, rho) if sd is None else sd
    z = norm.ppf(1 - alpha / 2) + norm.ppf(power)
    return math.ceil((z * spread / edge) ** 2)


def edge_interval(
    pnl_per_close: Sequence[float],
    contracts_per_close: Sequence[float],
    draws: int = 5000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Size-weighted profit per contract with a 95% interval that resamples whole closes.
    This is the effect size every strategy report must state."""
    pnl = np.asarray(pnl_per_close, dtype=float)
    size = np.asarray(contracts_per_close, dtype=float)
    if pnl.shape != size.shape or pnl.size < 2 or size.sum() <= 0:
        raise ValueError("need matching per-close profit and contracts for 2+ closes")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, pnl.size, size=(draws, pnl.size))
    boot = pnl[idx].sum(axis=1) / np.maximum(size[idx].sum(axis=1), _EPS)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return float(pnl.sum() / size.sum()), float(lo), float(hi)


# --- report -------------------------------------------------------------------------

REPORT_SIZINGS = (Sizing("kelly", 0.25), Sizing("fraction", 0.01), Sizing("fraction", 0.02))
REPORT_LAYOUTS = ((6, 1), (24, 1), (24, 4))  # (closes per day, trades per close)
# Current evidence, cents per filled contract (docs/LONGSHOT_GAPS.md).
EVIDENCE = (
    ("Paper long-shot account, 172 trades (size-weighted)", -1.06),
    ("Backfill, filled firings, all 30 days", -1.06),
    ("Backfill, filled firings, upper 95% bound", -0.21),
    ("Backfill, 15-60 min rule (live now), earlier closes", -0.44),
    ("Backfill, 15-60 min rule (live now), later closes", -1.89),
    ("Backfill, best upper 95% bound of any filled rule variant", 0.63),
    ("Backfill, all quoted firings incl. never-filled (unreachable)", 0.43),
)


def _cents(edge: float | None) -> float | None:  # pragma: no cover
    return None if edge is None else round(edge * 100, 2)


def report(budget: float = 500.0, paths: int = 1000) -> dict[str, Any]:  # pragma: no cover
    bar = Bar()
    net = Caps.saved_budget(budget, accounting="net")
    ledger = Caps.saved_budget(budget, accounting="ledger")
    pilot = Caps(budget, per_close=5.0, accounting="net")  # longshot_event_cents default
    payoffs: dict[str, Callable[[float], TradeMix]] = {
        "long-shot seller (maker, 86-97c)": longshot_seller,
        "50/50 taker (50c, 2c fee)": coinflip_taker,
    }
    quarter = Sizing("kelly", 0.25)
    required = []
    for name, make in payoffs.items():
        for closes, per in REPORT_LAYOUTS:
            row: dict[str, Any] = {
                "payoff": name,
                "trades_per_day": closes * per,
                "closes_per_day": closes,
                "trades_per_close": per,
            }
            kw: dict[str, Any] = {
                "closes_per_day": closes,
                "trades_per_close": per,
                "paths": paths,
                "hi": 0.30,
            }
            for sizing in REPORT_SIZINGS:
                edge = required_edge(make, sizing, net, bar, **kw)
                row[sizing.label] = _cents(edge)
                if edge is not None:
                    row[f"closes needed @ {sizing.label}"] = closes_needed(make(edge), edge, per)
            row["0.25x Kelly, $5 per-close cap"] = _cents(
                required_edge(make, quarter, pilot, bar, **kw)
            )
            row["0.25x Kelly, ledger as built"] = _cents(
                required_edge(make, quarter, ledger, bar, **kw)
            )
            required.append(row)
    data_needed = []
    for name, make in payoffs.items():
        for cents in (0.5, 1.0, 2.0, 3.0, 5.0):
            mix = make(cents / 100)
            edge = mix.edge
            data_needed.append(
                {
                    "payoff": name,
                    "edge_cents": round(edge * 100, 2),
                    "per_contract_sd_cents": round(mix.sd * 100, 1),
                    "closes_ci_excludes_zero_1_trade": closes_needed(mix, edge, 1, power=0.5),
                    "closes_80pct_power_1_trade": closes_needed(mix, edge, 1),
                    "closes_80pct_power_4_trades_rho1": closes_needed(mix, edge, 4, 1.0),
                    "closes_80pct_power_4_trades_rho0": closes_needed(mix, edge, 4, 0.0),
                }
            )
    evidence = []
    believer = (Sizing("fraction", 0.01), Sizing("fraction", 0.02))
    for name, cents in EVIDENCE:
        mix = longshot_seller(cents / 100)
        evidence.append(
            {
                "evidence": name,
                "edge_cents": cents,
                "gross_loss_rate_cents": round(mix.gross_loss_rate * 100, 2),
                "net": {
                    s.label: asdict(simulate(Scenario(mix, s, net, 24, 1), bar, paths))
                    for s in believer
                },
                "ledger_1_contract": asdict(
                    simulate(Scenario(mix, Sizing("contracts", 1), ledger, 24, 1), bar, paths)
                ),
                "ledger_2pct": asdict(
                    simulate(Scenario(mix, believer[1], ledger, 24, 1), bar, paths)
                ),
            }
        )
    return {
        "budget": budget,
        "bar": asdict(bar),
        "paths": paths,
        "required_edge": required,
        "data_needed": data_needed,
        "evidence": evidence,
    }


def main() -> None:  # pragma: no cover - research entry point
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budget", type=float, default=500.0)
    parser.add_argument("--paths", type=int, default=1000)
    parser.add_argument("--out", type=Path, default=Path("reports/bankroll-bar.json"))
    args = parser.parse_args()
    result = report(args.budget, args.paths)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
