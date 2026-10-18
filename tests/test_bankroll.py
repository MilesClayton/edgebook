from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.execution.live_ledger import Limits
from edgebook.research.bankroll import (
    FEE_RESERVE,
    Bar,
    Caps,
    Payoff,
    Scenario,
    Sizing,
    TradeMix,
    close_sd,
    closes_needed,
    coinflip_taker,
    edge_interval,
    longshot_seller,
    required_edge,
    simulate,
)

MAKER, TAKER = LiquidityRole.MAKER, LiquidityRole.TAKER
SHORT = Bar(horizon_days=5)


def mix_of(price: float, win: float, role: LiquidityRole = TAKER) -> TradeMix:
    return TradeMix((Payoff(price, win, role),), (1.0,))


def test_payoff_fee_matches_kalshi_schedule() -> None:
    for price, role in [(0.5, TAKER), (0.92, MAKER), (0.03, TAKER)]:
        p = Payoff(price, 0.5, role)
        assert p.fee == float(trade_fee(1, Decimal(f"{price:.4f}"), role))
    p = Payoff(0.5, 0.55, TAKER)
    taker = float(trade_fee(1, Decimal("0.5"), TAKER))  # 1.75c before Kalshi's rounding
    assert p.fee == pytest.approx(taker)
    assert 0.0175 <= taker <= 0.02
    assert p.cost == pytest.approx(0.5 + taker)
    assert p.edge == pytest.approx(0.05 - taker)
    assert p.reserve == pytest.approx(0.5 + 0.0276)
    assert pytest.approx(0.0276) == FEE_RESERVE


def test_at_edge_spreads_edge_by_upside() -> None:
    assert Payoff.with_edge(0.9, 0.01, MAKER).edge == pytest.approx(0.01)
    seller = longshot_seller(0.02)
    assert seller.edge == pytest.approx(0.02)
    upside = [1 - p.cost for p in seller.payoffs]
    ratios = [p.edge / u for p, u in zip(seller.payoffs, upside, strict=True)]
    assert max(ratios) == pytest.approx(min(ratios))  # same Kelly fraction everywhere
    assert all(p.win_prob < 1 for p in seller.payoffs)
    with pytest.raises(ValueError, match="mean upside"):
        longshot_seller(0.09)  # above 1 - mean price (8.25c) under any fee
    with pytest.raises(ValueError, match="win_prob"):
        Payoff.with_edge(0.97, 0.05, MAKER)


@pytest.mark.parametrize(("price", "win"), [(0.0, 0.5), (1.0, 0.5), (0.5, 1.2)])
def test_payoff_rejects_bad_inputs(price: float, win: float) -> None:
    with pytest.raises(ValueError, match="must be"):
        Payoff(price, win)


def test_mix_statistics() -> None:
    mix = coinflip_taker(0.01)
    flip_cost = 0.5 + float(trade_fee(1, Decimal("0.5"), TAKER))
    flip_win = flip_cost + 0.01
    assert mix.edge == pytest.approx(0.01)
    assert mix.win_prob == pytest.approx(flip_win)
    assert mix.price == pytest.approx(0.5)
    assert mix.sd == pytest.approx(math.sqrt(flip_win * (1 - flip_win)))
    assert mix.gross_loss_rate == pytest.approx((1 - flip_win) * flip_cost)
    seller = longshot_seller(0.0)
    assert 0.9 < seller.win_prob < 0.96
    assert seller.edge == pytest.approx(0.0, abs=1e-12)
    # sd also counts the price draw, so it beats any single-price sd at the mean
    rng = np.random.default_rng(1)
    k = rng.integers(0, 4, 400_000)
    win = np.array([p.win_prob for p in seller.payoffs])[k]
    cost = np.array([p.cost for p in seller.payoffs])[k]
    pnl = (rng.random(k.size) < win) - cost
    assert seller.sd == pytest.approx(float(pnl.std()), rel=0.02)


def test_mix_validation() -> None:
    p = Payoff(0.5, 0.5)
    with pytest.raises(ValueError, match="one weight"):
        TradeMix((p,), (0.5, 0.5))
    with pytest.raises(ValueError, match="sum to 1"):
        TradeMix((p,), (0.5,))
    assert TradeMix.at_edge([0.4, 0.6], 0.0, TAKER, [0.25, 0.75]).weights == (0.25, 0.75)


def test_sizing_rules() -> None:
    wealth = np.array([100.0, 200.0])
    win = np.array([0.6, 0.6])
    cost = np.array([0.52, 0.52])
    assert Sizing("contracts", 3).contracts(wealth, win, cost, 1).tolist() == [3, 3]
    # 2% of $100 split over 2 trades = $1 -> 1 contract at 52c
    assert Sizing("fraction", 0.02).contracts(wealth, win, cost, 2).tolist() == [1, 3]
    kelly = (0.6 - 0.52) / 0.48
    expected = np.floor(0.5 * kelly * wealth / 0.52)
    assert Sizing("kelly", 0.5).contracts(wealth, win, cost, 1).tolist() == expected.tolist()
    assert Sizing("kelly", 1).contracts(wealth, win - 0.2, cost, 1).tolist() == [0, 0]
    assert Sizing("contracts", 2).label == "2 contracts"
    assert Sizing("fraction", 0.02).label == "2% of wealth per close"
    assert Sizing("kelly", 0.25).label == "0.25x Kelly"
    with pytest.raises(ValueError, match="positive"):
        Sizing("fraction", 0)


def test_caps_mirror_saved_budget_and_limits() -> None:
    caps = Caps.saved_budget(50)
    assert (caps.budget, caps.order_debit, caps.open_exposure, caps.realized_loss) == (
        50,
        50,
        50,
        50,
    )
    assert caps.max_contracts == 10000
    assert caps.accounting == "ledger"
    legacy = Limits(
        purchase_spend_cents=500,
        open_exposure_cents=300,
        realized_loss_cents=300,
        order_debit_cents=100,
        max_contracts=1,
    )
    with pytest.raises(ValueError, match="capital-budget"):
        Caps.from_limits(legacy)
    limited = Caps.from_limits(legacy.model_copy(update={"capital_budget_cents": 500}), 250, "net")
    assert (limited.budget, limited.order_debit, limited.per_close) == (5, 1, 2.5)
    with pytest.raises(ValueError, match="positive"):
        Caps(0)


def test_validation_of_bar_and_scenario() -> None:
    with pytest.raises(ValueError, match="target"):
        Bar(target_multiple=1.0)
    mix = coinflip_taker(0.0)
    with pytest.raises(ValueError, match="at least one"):
        Scenario(mix, Sizing("contracts", 1), Caps(10), closes_per_day=0)
    with pytest.raises(ValueError, match="rho"):
        Scenario(mix, Sizing("contracts", 1), Caps(10), rho=1.5)
    assert Scenario(mix, Sizing("contracts", 1), Caps(10), 24, 3).trades_per_day == 72


def test_sure_winner_doubles_on_schedule() -> None:
    # 10 contracts at 50c that always win: +$4.80/close at a 2c fee; $100 -> $200 in 21 closes
    sc = Scenario(mix_of(0.5, 1.0), Sizing("contracts", 10), Caps(100, accounting="net"), 24, 1)
    res = simulate(sc, SHORT, paths=50)
    gain = 10 * (0.5 - float(trade_fee(1, Decimal("0.5"), TAKER)))
    assert res.p_target == 1.0
    assert res.p_ruin == res.p_stalled == 0.0
    assert res.median_days_to_target == 1
    assert res.median_final_multiple == pytest.approx((100 + math.ceil(100 / gain) * gain) / 100)
    assert res.passes


def test_sure_loser_is_ruined() -> None:
    sc = Scenario(mix_of(0.5, 0.0), Sizing("fraction", 0.1), Caps(100, accounting="net"), 24, 1)
    res = simulate(sc, SHORT, paths=50)
    assert res.p_ruin == 1.0
    assert res.p_target == 0.0
    assert res.median_days_to_target is None
    assert not res.passes


def test_ledger_gross_loss_cap_stalls_a_profitable_strategy() -> None:
    # A 10c edge on a 50/50 still loses ~38c gross per contract; the ledger stops once
    # gross losses reach the budget, long before net profit doubles it.
    mix = coinflip_taker(0.10)
    sizing = Sizing("contracts", 5)
    ten = Bar(horizon_days=10)
    ledger = simulate(Scenario(mix, sizing, Caps.saved_budget(50), 24, 1), ten, 400)
    net = simulate(Scenario(mix, sizing, Caps.saved_budget(50, "net"), 24, 1), ten, 400)
    assert ledger.p_stalled > 0.75
    assert ledger.p_target < 0.25
    assert net.p_target > 0.9


def test_caps_bound_contracts() -> None:
    mix = mix_of(0.5, 0.5)
    huge = Sizing("contracts", 1000)
    one_day = Bar(horizon_days=1, ruin_fraction=1.0, target_multiple=100)
    for caps, per_trade in [
        (Caps(1000, max_contracts=3, accounting="net"), 3),
        (Caps(1000, order_debit=2.0, accounting="net"), 3),  # 2 / 0.5276 -> 3
        (Caps(1000, per_close=1.1, accounting="net"), 2),
        (Caps(1000, open_exposure=1.1, accounting="net"), 2),
    ]:
        res = simulate(Scenario(mix, huge, caps, 24, 1), one_day, paths=20)
        assert res.contracts_per_day == pytest.approx(24 * per_trade)
    # the per-close room is shared by every trade in the close
    res = simulate(
        Scenario(mix, huge, Caps(1000, per_close=1.1, accounting="net"), 24, 4), one_day, 20
    )
    assert res.contracts_per_day == pytest.approx(24 * 2)


def test_comonotone_closes_add_no_diversification() -> None:
    mix = mix_of(0.5, 0.5)
    assert close_sd(mix, 4, 1.0) == pytest.approx(0.5, rel=0.02)
    assert close_sd(mix, 4, 0.0) == pytest.approx(0.25, rel=0.02)
    # a ladder of comonotone trades loses together: worse odds than independent ones
    sizing = Sizing("fraction", 0.4)
    bar = Bar(horizon_days=3)
    together = simulate(
        Scenario(coinflip_taker(0.0), sizing, Caps(100, accounting="net"), 4, 4, 1.0), bar, 400
    )
    apart = simulate(
        Scenario(coinflip_taker(0.0), sizing, Caps(100, accounting="net"), 4, 4, 0.0), bar, 400
    )
    assert together.p_ruin > apart.p_ruin


def test_required_edge_search() -> None:
    caps = Caps(100, accounting="net")
    kw: dict[str, Any] = {"closes_per_day": 24, "paths": 200, "step": 0.005}
    edge = required_edge(coinflip_taker, Sizing("kelly", 0.25), caps, SHORT, **kw)
    assert edge is not None
    assert 0 < edge < 0.10
    sc = Scenario(coinflip_taker(edge), Sizing("kelly", 0.25), caps, 24, 1)
    assert simulate(sc, SHORT, 200).passes
    # one contract a trade can never double $100 in five days at <= 10c a contract
    assert required_edge(coinflip_taker, Sizing("contracts", 1), caps, SHORT, **kw) is None
    # infeasible edges end the scan instead of raising
    assert required_edge(longshot_seller, Sizing("contracts", 1), caps, SHORT, **kw) is None


def test_closes_needed() -> None:
    mix = mix_of(0.5, 0.5)
    sd = 0.5
    z = 1.959963984540054 + 0.8416212335729143
    assert closes_needed(mix, 0.01, sd=sd) == math.ceil((z * sd / 0.01) ** 2)
    assert closes_needed(mix, 0.01, sd=sd, power=0.5) == math.ceil((1.959963984540054 * 50) ** 2)
    assert closes_needed(mix, 0.02, 4, 0.0) < closes_needed(mix, 0.02, 4, 1.0)
    with pytest.raises(ValueError, match="positive"):
        closes_needed(mix, 0.0)


def test_edge_interval_resamples_closes() -> None:
    pnl = [0.5, -0.3, 0.2, 0.1, -0.1, 0.4]
    size = [10, 10, 5, 5, 10, 10]
    est, lo, hi = edge_interval(pnl, size)
    assert est == pytest.approx(0.8 / 50)
    assert lo < est < hi
    with pytest.raises(ValueError, match="matching"):
        edge_interval([1.0], [1.0])
