# The bankroll bar: what "safely double the budget" requires — 2026-10-18

## Question

The owner's goal is "not insane profits, but enough to safely 2x the set budget". This
document turns that into pass/fail criteria, shows what true edge, trade count and sizing
meet them, and says how much data it takes to show an edge that size is real. Every edge
idea should report against this bar (see [the rule](#the-rule-every-research-result-reports-against)).

Code: `src/edgebook/research/bankroll.py` (simulator, tests in `tests/test_bankroll.py`).
Tables: `uv run python -m edgebook.research.bankroll --paths 2000` (about 80 s; writes
`reports/bankroll-bar.json`, git-ignored). Offline only; nothing here touches live trading.

## The bar

A strategy **passes** when, starting from the saved budget B and trading for 90 days:

| Criterion | Default | `Bar` field |
|---|---|---|
| P(wealth reaches 2 × B within the horizon) | **≥ 50%** | `target_multiple=2.0`, `min_p_target=0.50`, `horizon_days=90` |
| P(wealth falls to 0.5 × B before reaching 2 × B) | **≤ 5%** | `ruin_fraction=0.50`, `max_p_ruin=0.05` |

All five are parameters. Reaching the target or the ruin level ends a path (the owner
would stop there either way).

## How it is simulated

- **Payoff.** Each trade buys one side of a $1 binary at price c, wins with true
  probability p, and pays the Kalshi fee from `engine.decide.fee` (rounded up per
  contract). The true edge per contract after fees is `p − c − fee`.
  - *Long-shot seller:* maker buys of the favourite at 86, 90, 94 or 97¢ (the long shot
    offered at 3–14¢). At zero edge it wins 92.75% of the time. The edge is spread in
    proportion to each price's upside `1 − c − fee`, so a 97¢ contract never becomes a
    sure thing. The most edge this mix can carry is 7.25¢.
  - *50/50 taker:* buys at 50¢ and pays the 2¢ taker fee. It wins 52% + edge.
- **Dependence.** Trades are grouped into closes. A Gaussian copula with latent
  correlation ρ links the trades of one close. All tables use **ρ = 1**: every strike of a
  close moves with the same BTC print, which is the worst case for a ladder.
- **Sizing** (based on wealth at the start of each close, split across its trades):
  quarter Kelly (`0.25 × (p − cost)/(1 − cost)` of wealth, computed from the *true* edge),
  or a fixed 1% or 2% of wealth at risk per close. Contracts are whole numbers.
- **Caps.** These are the ledger's money limits. Every order reserves `price + 2.76¢`
  (the ledger's worst-case fee reserve) against the budget, open-exposure, per-order,
  per-close and realized-loss caps (`Caps`, built from `Limits`). The tables use a
  **$500 budget** with every cap set to the budget, which is what the dashboard's
  *Save budget* does.
- **Two accounting modes.** `net`: losses count as drawdown below B, and the stake grows
  with wealth. This is what compounding to 2x means. `ledger`: the ledger as built (see
  the next section).

## Structural finding: the live ledger cannot double a budget

The ledger's realized-loss counter is **gross**. `state["loss"] += max(0, -pnl)` runs on
every losing trade, and wins never refund it (`docs/LIVE_EXECUTION.md`, "Budget
semantics"). Under *Save budget*, the capital allowance and the realized-loss cap both
equal B. So trading stops for good once the losing trades add up to B, however much the
winners made.

To net +B before the losers add up to B, the true edge per contract has to exceed the
expected *gross* loss per contract, `(1 − p) × cost`:

| Payoff | Gross loss per contract at zero edge | Edge needed to double under the ledger (0.25× Kelly, 24 trades/day) |
|---|---:|---:|
| Long-shot seller | 6.6¢ | **3.5¢** (gross loss there: 3.4¢) |
| 50/50 taker | 25.0¢ | **16.75¢** (gross loss there: 16.3¢) |

No strategy studied so far comes anywhere close. In simulation, every current long-shot
estimate sized at 2% of wealth **stalls on the loss cap in ≥99.9% of paths** before
doubling. At 1 contract per trade it never stalls but also never gets near 2x.

**This is a decision for the owner, not something to change in research.** "Safely
double" needs either a cap measured as net drawdown (for example, stop if wealth falls
below 0.5 × B) or a deliberate top-up of the allowance as profits accrue. Until then, any
edge found below has to be judged in `net` terms while knowing the live account would
stop early. The tables below use `net`.

## Required edge (net accounting, $500, ρ = 1)

Smallest true edge per filled contract, after fees, that passes the bar. The search is a
0.25¢ grid with 2,000 paths, so read each cell as ±0.25¢. "—" means no edge the payoff
can carry passes.

| Payoff | Trades/day (closes × per close) | 0.25× Kelly | 1% of wealth / close | 2% of wealth / close | 0.25× Kelly with the pilot's $5 per-close cap |
|---|---|---:|---:|---:|---:|
| Long-shot seller | 6 (6 × 1) | 2.00¢ | — | 6.00¢ | — |
| Long-shot seller | 24 (24 × 1) | **1.00¢** | 3.25¢ | 1.50¢ | 4.75¢ |
| Long-shot seller | 96 (24 × 4) | 1.00¢ | 4.25¢ | 1.75¢ | 4.75¢ |
| 50/50 taker | 6 (6 × 1) | 3.75¢ | 7.00¢ | 3.50¢ | 10.25¢ |
| 50/50 taker | 24 (24 × 1) | **2.00¢** | 1.75¢ | 2.50¢ | 2.50¢ |
| 50/50 taker | 96 (24 × 4) | 2.00¢ | 2.00¢ | 2.25¢ | 2.50¢ |

What the table says:

- **About 1¢ per filled contract for a seller and 2¢ for a 50/50 taker** is the floor,
  at roughly one trade per hourly close and quarter-Kelly sizing. At that floor the seller
  doubles in a median of 57 days (P = 65%, P(ruin) = 0.5%), trading about 420 contracts a
  day. That is about 17 contracts per hourly close on a $500 budget, so liquidity at the
  quoted price has to support it. At 2¢ it takes 19 days (P = 99%).
- **More trades in the same close barely help.** With ρ = 1, four strikes on one close
  are close to one big bet. Spreading over more *closes* helps: at 6 closes a day the
  needed edge doubles.
- **Half Kelly fails at every edge.** It loses half the budget first 5–9% of the time
  (4,000 paths, 1–4¢ seller edges). That matches the continuous-time figure of about 11%.
  Quarter Kelly stays under 1%.
- **The pilot's $5-per-close limit and 1-contract orders cap throughput.** On a $500
  budget the seller would need 4.75¢ to double in 90 days. The caps protect the account
  but also rule out doubling it at any realistic edge.
- Kelly here is sized on the *true* edge. A real strategy only has an estimate, so these
  are lower bounds on the edge needed.

## Data needed to tell that edge from zero

Independent closes needed before a true edge is distinguishable from zero, resampling
whole closes and trading one contract per trade (ρ = 1 unless noted).
"CI excludes 0" means the 95% interval just excludes zero when the true edge is as stated
(50% power). "80% power" means a two-sided 5% test detects it four times out of five.

| Payoff | True edge | SD per contract | Closes: CI excludes 0 | Closes: 80% power | 80% power, 4 trades/close, ρ=1 | 4 trades/close, ρ=0 |
|---|---:|---:|---:|---:|---:|---:|
| Long-shot seller | 0.5¢ | 24.8¢ | 9,439 | 19,285 | 14,568 | 4,813 |
| Long-shot seller | **1.0¢** | 23.9¢ | **2,210** | **4,516** | 3,408 | 1,125 |
| Long-shot seller | 2.0¢ | 22.1¢ | 470 | 961 | 726 | 241 |
| Long-shot seller | 3.0¢ | 20.1¢ | 172 | 351 | 266 | 89 |
| Long-shot seller | 5.0¢ | 15.0¢ | 35 | 72 | 53 | 18 |
| 50/50 taker | 1.0¢ | 49.9¢ | 9,570 | 19,553 | 19,553 | 4,916 |
| 50/50 taker | **2.0¢** | 49.8¢ | **2,386** | **4,875** | 4,875 | 1,225 |
| 50/50 taker | 3.0¢ | 49.7¢ | 1,057 | 2,159 | 2,159 | 542 |
| 50/50 taker | 5.0¢ | 49.5¢ | 377 | 770 | 770 | 193 |

At 24 hourly closes a day, confirming the 1¢ seller floor takes **about 190 days** of
filled trades, or about 92 days just to get an interval that excludes zero. The 30-day
backfill has about 720 hourly closes; its 422-close test set gives intervals of roughly
±2¢. An idea that only clears 1–2¢ cannot be confirmed on one month of data. It has to
show a bigger edge, or wait for more closes.

## Current evidence against the bar

Long-shot estimates are in ¢ per filled contract (`docs/LONGSHOT_GAPS.md`). They were
simulated at 24 closes a day, $500, ρ = 1, sizing as a believer in the strategy would
(Kelly declines to bet at zero or negative edge).

> **Fee correction pending (`docs/KALSHI_INCENTIVES.md`, the `research/kalshi-incentives`
> branch):** BTC series charge makers no fee, but the backfill estimates below charged 1¢ per
> filled sale, so each backfill row is 1.00¢ too low. Corrected, all filled firings are
> −0.06¢ and the 15–60 minute rule is +0.56¢ on earlier closes and −0.89¢ on later ones.
> All of these are still below the ~1¢ the bar needs, so the verdict stands. This simulator
> also takes its fees from `engine.decide.fee`, so the required-edge tables shift slightly
> once that fix merges.

| Evidence | Edge | Net, 1%/close: P(2x) / P(ruin) / median end | Net, 2%/close: P(2x) / P(ruin) / median end | Ledger as built, 2%/close: P(stalled) |
|---|---:|---|---|---:|
| Paper long-shot account, 172 trades (size-weighted) | −1.06¢ | 0% / 0.05% / 0.79× | 0% / 29% / 0.60× | 100% |
| Backfill, filled firings, all 30 days | −1.06¢ | 0% / 0.05% / 0.79× | 0% / 29% / 0.60× | 100% |
| Backfill, filled firings, upper 95% bound | −0.21¢ | 0% / 0% / 0.95× | 0% / 1.7% / 0.89× | 100% |
| Backfill, 15–60 min rule (live now), earlier closes | −0.44¢ | 0% / 0% / 0.90× | 0% / 4.9% / 0.79× | 100% |
| Backfill, 15–60 min rule (live now), later closes | −1.89¢ | 0% / 1.1% / 0.67× | 0% / 79% / 0.50× | 99.9% |
| Best upper 95% bound of any filled rule variant | +0.63¢ | 0% / 0% / 1.15× | 6.5% / 0.05% / 1.32× | 100% |
| All quoted firings incl. never-filled (unreachable) | +0.43¢ | 0% / 0% / 1.10× | 2.5% / 0.15% / 1.20× | 100% |

**No current strategy meets the bar.** Every point estimate for filled long-shot sales is
negative. Even the most optimistic upper 95% bound of any rule (+0.63¢) is below the 1¢
needed, and at that edge doubling within 90 days happens 6.5% of the time, not 50%. The
paper account's 172 trades have a standard error of about 1.9¢ per contract, so they
cannot rule out zero or −3¢ either. The ML diagnostic (`reports/ML_DIAGNOSTIC_2026-10-17.md`)
found no forecast that beats Kalshi's price, so no 50/50 taker edge exists to test.

## The rule every research result reports against

Every edge study reports, on **untouched later closes** and on **filled** trades only (a
fill proxy is acceptable if it is labelled as one):

1. The **effect size per contract after fees, size-weighted, with a 95% interval that
   resamples whole closes**: `bankroll.edge_interval(pnl_per_close, contracts_per_close)`.
2. **Trades per day and trades per close.**
3. The **worst single close** in dollars per contract traded in it.
4. The verdict from `bankroll.simulate` at the proposed sizing and with the payoff's real
   price mix. It passes only if the bar is met at the **lower** end of the interval, not
   the point estimate.

A result whose interval's lower end is below the required edge in the table above (about
1¢ for seller-like and 2¢ for 50/50 payoffs at one trade per hourly close) is "not yet
shown", whatever its point estimate. The data-needed table says how many more closes it
would take.

## Caveats

- ρ = 1 is conservative for strategies that trade both sides of a close (an up long shot
  and a down long shot cannot both win). Use `Scenario(rho=...)` with a measured value.
- Closes are treated as independent. Regime clustering across hours (volatile days) would
  make real drawdowns worse than simulated.
- Fees use the 1-contract rounding for every contract, which is slightly conservative for
  multi-contract fills. Maker rebates and incentives are ignored.
- Grid steps are 0.25¢ and each cell uses 2,000 paths, so read each cell as ±1 step.
