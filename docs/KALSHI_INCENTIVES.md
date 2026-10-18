# Kalshi fees and incentive programs for BTC markets — 2026-10-18

## Question

Edgebook's strategies sit within a couple of cents of break-even. Can Kalshi's fee schedule
or an incentive program add the missing cents per contract on KXBTC15M (15-minute),
KXBTCD (hourly above/below) and KXBTC (hourly range), for a direct-member account
trading 1-contract post-only resting orders?

## Verdict

| Source | ¢ per filled contract, long-shot pilot | Status |
|---|---:|---|
| Maker fee on BTC series | **0** (the repo charged 1.00) | Verified: series `fee_type` is `quadratic` |
| Correcting the repo's maker fee | **+1.00** in research and paper P&L | Fixed in this branch. An accounting fix: real fills were already free |
| Taker fee rounding, direct member | +0.1 to +0.9 per 1-contract taker fill vs. whole-cent rounding (0.66 at 5¢, 0.25 at 50¢) | Fixed in this branch |
| Liquidity Incentive Program | **0** | No BTC-series program has ever run. If one did, a 1-contract order would earn about 0.4¢ per period, below the $1 payout minimum |
| Volume Incentive Program | **0** | Terminated Oct 13, 2026. KXBTC15M's run (Jan 26 – May 12, 2026) paid about 0.01¢ per contract |
| Volume-based fee tiers | 0 | Only perpetual futures have them |
| Market Maker Program | 0 | Two-sided quoting obligations; the only stated benefit is "reduced fees", which cannot lower a fee that is already 0 |
| Scheduled fee changes | none | `GET /series/fee_changes` has nothing upcoming for these series |

**Kalshi offers no money a 1-contract resting strategy can collect on these series.** The one
real gain is a correction. The repo charged a whole-cent maker fee that Kalshi does not
charge, so every long-shot P&L estimate was 1.00¢ per contract too low. Corrected, the
backfill's filled sales go from −1.06¢ to **−0.06¢** per contract (95% interval −0.97 to
+0.79): break-even, not profitable. The real-money pilot's ledger was never wrong. It records
the fees Kalshi actually reports, and those should already be zero (see "Owner checks").

## 1. What Kalshi publishes (official sources only)

### Fee formulas

- **Per-series fee type.** `GET /series/{ticker}` returns `fee_type` and `fee_multiplier`.
  The API reference ([Get Series](https://docs.kalshi.com/api-reference/market/get-series))
  says `quadratic` "is described by the General Trading Fees Table", and
  `quadratic_with_maker_fees` adds the schedule's Maker Fees section.
  `quadratic_with_combo_maker_fees` uses "a 0.5 maker multiplier instead of 0.25", and
  `flat` uses the Specific Trading Fees Table.
- **Live values, fetched 2026-10-18** from `api.elections.kalshi.com/trade-api/v2/series/…`:

  | Series | `fee_type` | `fee_multiplier` |
  |---|---|---:|
  | KXBTC15M | quadratic | 1 |
  | KXBTCD | quadratic | 1 |
  | KXBTC | quadratic | 1 |

  Of 278 Crypto-category series, 274 are `quadratic` ×1, 2 are `quadratic` ×0 and 2 are
  `quadratic_with_maker_fees`.
- **Formulas.** Taker fee = `M × 0.07 × C × P × (1 − P)`, rounded up. Maker fee =
  `M × 0.0175 × C × P × (1 − P)`, where 0.0175 is 0.25 × 0.07, charged only on maker-fee
  types. Sources: the [fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
  ("Fee Schedule for July 2026 – 7.7.26 Update") and the API's fee-type descriptions.
  **Partly unverified:** kalshi.com returned HTTP 429 every time, so I could not open the PDF
  itself. The coefficients and the maker multiplier come from the PDF's search-index text
  and the API docs' 0.25 / 0.5 maker multipliers. They match the coefficients the repo
  already used.
- **When maker fees apply.** The [Fees help article](https://help.kalshi.com/en/articles/13823805-fees)
  says some markets carry maker fees, that they are "only charged when a trade is ultimately
  executed", and that canceling a resting order costs nothing.
- **Event overrides.** `GET /events/fee_changes` lists per-event overrides. On 2026-10-18 it had
  19, all for sports, none for BTC. Execution already fails closed on any override
  (`execution/broker.py` `quote`).

### Rounding: direct members vs. everyone else

From [Fee Rounding](https://docs.kalshi.com/getting_started/fee_rounding):

1. trade fee = model fee rounded up to $0.000001;
2. the balance change is aligned down to the account's precision. "Direct member balances
   are aligned to `$0.0001`"; non-direct members, including FCM customers, are aligned to $0.01;
3. the alignment overpayment builds up per order and is rebated in steps of that precision,
   so over many fills of one order the account pays the trade fee almost exactly.

So a direct member's 1-contract taker fill at 5¢ costs $0.0034 (0.07 × 0.05 × 0.95 =
0.003325, rounded up). It does not cost $0.01.

### Incentive programs

All program data below comes from the public, unauthenticated
[`GET /incentive_programs`](https://docs.kalshi.com/api-reference/incentive-programs/get-incentives).
`period_reward` is in centi-cents. Programs pulled 2026-10-18: 5,612 active, 1,842 upcoming,
16,936 closed and 311,042 paid out.

**Liquidity Incentive Program (LIP).** Terms are in the
[help center](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program),
which says the program runs until Jan 1, 2027, and in the
[Feb 11, 2026 CFTC filing](https://www.cftc.gov/sites/default/files/filings/orgrules/26/02/rules02112639183.pdf),
Appendix A.

- Eligibility: Kalshi members except affiliates, members who have signed a Market Maker
  Agreement, and IBs or FCMs and their customers. The help center adds that non-U.S. users
  are excluded. A direct member like this account qualifies.
- Scoring: Kalshi takes one random snapshot of the book per second. A side counts only if
  resting size reaches the period's Target Size (100 to 20,000 contracts). An order scores
  `size × DiscountFactor^ticks` below the reference price. In the current help-center
  version, the reference is the level where cumulative size first reaches Target/5. Scores
  are normalised per side, and each snapshot is worth at most 2.0 across all users.
- Payout: your share of total scores × the period reward × the fraction of usable snapshots,
  rounded down to the cent. **Payouts under $1.00 are not paid.** Rewards are $1–$1,000 per
  market per day (the filing says at least $10 per day). Kalshi can end a program at any time.
- Enrollment: none. Rewards accrue to qualifying resting orders. Programs are shown on
  [kalshi.com/incentives](https://help.kalshi.com/en/articles/16076644-liquidity-and-volume-incentive-programs-where-to-find-them)
  and with a market's Rewards badge. A verified SSN is needed above IRS reporting thresholds.
- **BTC series: none.** No active, upcoming, closed or paid-out LIP has ever covered KXBTC15M,
  KXBTCD or KXBTC. The closest is **KXCRYPTOLEAD15M** ("which coin leads this 15 minutes"):
  25,772 paid-out periods since Aug 24, 2026, each paying $20 per 15-minute market with
  Target 1,000 and Discount 0.5. That is a different contract, which edgebook does not model.

**Volume Incentive Program.** The
[Sep 28, 2026 CFTC filing](https://www.cftc.gov/filings/orgrules/rules09282630083.pdf)
terminates it, effective no earlier than **Oct 13, 2026**. It excluded Market Maker Agreement
holders and FCM/IB customers. It split a fixed per-market reward by Eligible Volume, meaning
CLOB trades between $0.03 and $0.97. It was capped at "$.005 per contract traded for each
participant". KXBTC15M had 9,781 paid-out volume periods (Jan 26 – May 12, 2026) of $10–$20
each, and KXETH15M had a similar run. Those markets traded a median of about 170,000
contracts each (40 sampled), so the pool came to a median of **0.009¢ per contract**
(10th–90th percentile 0.006–0.022¢), before splitting between both sides of each trade.
KXBTCD and KXBTC never had one.

**Market Maker Program.** From
[How to Become a Market Maker](https://help.kalshi.com/en/articles/13823819-how-to-become-a-market-maker-on-kalshi):
approval follows a review of "financial resources, trading experience, and business
reputation". Market makers must meet "defined quoting and volume requirements" and quote both
sides consistently. In return they may get "reduced fees" and adjusted position limits. The
page lists KXBTC among current market-maker products. **Not verified:** the page gives no
application route, quoting thresholds or fee terms. A signed agreement also excludes the
account from the LIP and the volume program.

**Volume tiers.** [Get Fee Tiers](https://docs.kalshi.com/margin-rest/fees/get-fee-tiers) and
the schedule's basis-point tier table apply to perpetual futures only. So does the
[temporary perpetual fee rebate](https://cftc.gov/filings/orgrules/rules0625267305.pdf).
I found no volume tier for event contracts.

## 2. How the repo compared

| Code | Before | Kalshi | Fix |
|---|---|---|---|
| `core.fees.trade_fee` default schedule | maker 0.0175, rounded up to $0.01 per fill | maker 0 on BTC series; direct-member alignment $0.0001 | Default `KALSHI_BTC` (maker 0, $0.0001). `FeeSchedule.from_series(fee_type, multiplier, direct_member)` maps API fields; `KALSHI_WITH_MAKER_FEES` and `precision=0.01` cover other cases |
| `engine.decide.fee` / `fill_fee` (paper engine, all strategies) | inherited the above: 1¢ per 1-contract maker fill; taker 1–2¢ | maker 0; taker exact to $0.0001 | inherited |
| `research.edge_scan.taker_fee` | 100-lot taker, total rounded to $0.01 | same formula, $0.0001 | inherited; under 0.01¢ per contract change at 100 lots |
| `research.longshot_gaps.seller_pnl` | −1¢ maker fee on every filled sale | 0 | inherited |
| `execution.live_ledger.fee_reserve` | $0.0276 per contract | a bound, not an estimate | **No change.** It assumes coefficient ≤ 0.07, 0.01-contract fills, $0.000001 + $0.0001 rounding per fill, direct member. That is correct and conservative for both roles |
| `execution.service` candidate edge | subtracts `fee_reserve` as the fee | true taker fee 0.34–1.76¢ | **Follow-up, not changed.** It overstates the fee by 1–2.4¢ and blocks marginal autopilot entries. It is a safety gate, so changing it is a separate decision |
| `execution.broker.quote` fee guard | accepts `quadratic` and `quadratic_with_maker_fees` with multiplier ≤ 1; rejects changes within 60 s | consistent | no change |

The fix is in its own commit: `fix(fees): BTC series charge makers nothing; direct members
round to $0.0001`. New tests cover the BTC maker = 0 rule, direct vs. non-direct rounding, the
fee-rounding doc example, every `fee_type` in `from_series`, and property tests that direct
fees fall between the exact fee and whole-cent rounding. Eight older tests had whole-cent
fees hard-coded and were updated.

## 3. Long-shot backfill re-run (`research.longshot_gaps`, branch `research/longshot-gaps`)

Same data (Sep 16 – Oct 16, 2026), same seed. The only code difference is the fee fix.

| Result (¢ per filled contract) | Old fee | Corrected | 95% interval, corrected |
|---|---:|---:|---:|
| All filled firings | −1.06 | **−0.06** | −0.97 to +0.79 |
| Unfilled firings (no fee either way) | +3.61 | +4.61* | +4.30 to +4.89 |
| Test: sell every firing | −2.42 | −1.42 | −3.39 to +0.42 |
| Test: current paper rule | −2.49 | −1.49 | −3.60 to +0.44 |
| Test: gradient-boosted filter | −2.71 | −1.71 | −3.87 to +0.42 |
| Test: logistic filter (cut-off re-chosen: 3¢ → 1.5¢) | −3.86 (504 mkts) | −1.30 (1,073 mkts) | −3.86 to +0.93 |
| Rule variants, earlier / later closes | | | |
| Current (5–60 min, both series) | −1.07 / −2.42 | −0.07 / −1.42 | |
| 15–60 min left (now live) | −0.44 / −1.89 | **+0.56 / −0.89** | −0.63 to +1.63 / −2.97 to +1.17 |
| 15–60 min, ask ≤ 6¢ | −0.50 / −1.29 | +0.50 / −0.29 | |
| 15–60 min, distance ≥ 1.5σ | −0.49 / −1.64 | +0.51 / −0.64 | |

\*The study charges a hypothetical fee on unfilled firings as if they had filled, so this row
moves too.

Each row moves by exactly +1.00¢, because the old helper charged a full cent on every 1-contract
maker fill. Rankings and the ML conclusions stay the same. Kalshi's price still wins, and the
filters still do not beat it. **The pilot's current rule is now roughly break-even on earlier
closes and still negative on the later, untouched test closes. None of the intervals clears
zero.** The paper long-shot account's maker fills are large, so its fee was about
0.0175 × P(1−P) ≈ 0.1–0.2¢ per contract, not 1¢. Its size-weighted P&L in
`LONGSHOT_GAPS.md` therefore improves by about +0.1–0.2¢ only. Estimated, not re-run: the
account file is on the recorder.

Other sessions price fills with the same helper (`seller_pnl`, `taker_fee`, the 1-contract
"fee rounding" variant). Any 1-contract maker result computed before this fix is 1¢ too
pessimistic. A 1-contract taker result is 0.1–0.9¢ too pessimistic, depending on price.

## 4. What an incentive could pay this account

The pilot rests 1 contract per order on the favourite. That is a few hundred orders a day,
each resting for minutes before it fills or expires 15 minutes before close.

- **LIP on BTC series: 0¢.** There is no program, and there never has been one. Suppose
  Kalshi added a KXCRYPTOLEAD15M-style program ($20 per 15-minute market, Target 1,000).
  On 2026-10-18 the best bid on KXBTC15M and on KXBTCD favourites held about 2,000–4,300
  contracts, so a 1-contract order at the best price would hold about 1/2,500 of one side
  and 1/5,000 of the snapshot. Resting the whole period earns $20 / 5,000 ≈ **0.4¢ per period**,
  below the **$1.00 minimum**, so it pays nothing. Clearing $1 takes about 5% of the
  qualifying score: roughly 125–250 contracts resting on *both* sides for the whole period.
  That is a two-sided market-making book far beyond the pilot's $3 loss cap, with the adverse
  selection `LONGSHOT_GAPS.md` measures.
- **Volume program: 0¢.** It has ended. At its peak, KXBTC15M's pool came to about 0.01¢ per
  contract traded, and the cap was 0.5¢.
- **Market Maker Program: 0¢.** The maker fee is already 0, the obligations are two-sided
  and continuous, and joining would forfeit the LIP.

**Bottom line: about 0¢ per contract from incentives, and 0¢ from fees beyond the correction.
No Kalshi program closes the gap or supports doubling the budget.**

## Owner checks and steps (nothing was applied for or sent)

1. **Confirm the zero maker fee on real fills.** On the dashboard's **Real positions**, or in
   the recorder ledger (`orders.fees`, the fills' `fee`), check that the pilot's resting-order
   fills on KXBTCD/KXBTC15M show $0.0000 maker fees. Kalshi reports them as
   `maker_fees_dollars`. This session could not read production (fly ssh was refused).
2. **Watch for a BTC LIP.** Check [kalshi.com/incentives](https://kalshi.com/incentives) or the
   Rewards badge on KXBTCD/KXBTC15M markets. Or poll
   `GET /incentive_programs?status=upcoming` for those tickers. It is public, and the recorder
   could log it. A program only matters with ≥ 125 contracts resting on both sides.
3. **Watch for fee changes.** `GET /series/fee_changes?series_ticker=KXBTCD` (and the others).
   The broker already refuses quotes within 60 s of a change. A switch to
   `quadratic_with_maker_fees` would bring back a maker fee, but only 0.03–0.22¢ at 85–98¢ with direct precision, not 1¢.
4. **Market Maker Program**, only if the strategy becomes two-sided market making: the help
   page gives no application route. Contact Kalshi through the help center after reading the
   Market Maker Agreement terms on [regulatory notices](https://kalshi.com/regulatory/notices).
   Not recommended at this size.
5. Keep the account a **direct member**. As an FCM or IB customer it would lose LIP
   eligibility and pay whole-cent alignment: 1¢ per 1-contract fill instead of 0.34¢ for a 5¢ taker.
