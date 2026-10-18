# Confirmed Kalshi execution

See [the validation record](EXECUTION_VALIDATION.md) for checks, fixes and what has been verified in production.

The Python recorder now has a real order adapter and a durable execution ledger. The Chrome extension asks Vercel for a Jev decision, displays an unsubmitted proposal, and submits only after the user confirms its exact terms. Everything ships disabled. This implementation has not been connected to a funded account or exercised against the real broker or model service.

## Current popup controls

**Temporary interruptions recover automatically.** A network timeout, rate limit, broker 5xx response, or stale account read interrupts an already running automatic strategy without canceling its authorization. The recorder retries every five seconds and resumes after three spaced successful account reconciliations. Both views show RECOVERING and the clean-check count. Any failed check resets the count. Manual Stop, switching both strategies off, an unresolved order, account mismatch, failed cancellation, and server restarts remain paused until owner action. Recovery never submits an order itself; the existing strategy performs its normal checks on its next pass.

The popup has **Live** and **Help & details** tabs. Save a dollar budget, choose Jev, long-shot, or both, then press **Start live trading**. Saving a budget pauses execution and cancels resting bids; it never activates trading. Start atomically configures the approach, reconciles, and resumes. Stop is available during other requests and blocks new authorizations while canceling resting orders. Existing fills remain open.

The capital budget is reusable: open cost and fee reservations plus gross realized losses must fit the saved allowance. Closing positions releases capital; losses continue to consume the allowance. Lifetime buy spending is still recorded and is never reset. Legacy cumulative-spend caps remain available in advanced controls. Server freshness, account reconciliation, fee bounds, and sufficient executable liquidity remain mandatory.

In explicit capital-budget mode Jev receives executable candidates without the legacy fixed edge/exit threshold veto. It chooses buy, hold, skip, or exit and small (one contract), medium (half), or maximum affordable size for the chosen side. Quotes and actual depth determine each side's maximum separately. This is model discretion, not evidence of profit. Legacy manual reviews retain their fixed edge rules. All submitted terms are revalidated and reserved under the latest owner limits.

Jev checks supported BTC 15-minute and hourly markets and all routed holdings, including fractional fills and holdings near close. Extra browser tabs are unnecessary; execution runs on the recorder. The long-shot mode still uses its configured small post-only bid size and per-close limit. Use **Real positions** on the dashboard for reconciled holdings, resting orders, actual fills and fees, separate from the paper accounts.

The paper-proof gate now uses daily blocks and a Student-t confidence interval, so correlated strikes and partial exits do not count as independent evidence. It remains separately configurable; a waiver is shown and audited. Offline passing tests do not validate real broker behavior or profit.

## Components

- Existing recorder host: live engine, Kalshi credentials, authenticated execution API, reconciliation, SQLite ledger and audit trail on the existing durable volume.
- Vercel dashboard: password authentication, Jev through AI Gateway, and a fixed authenticated proxy to the recorder. Its filesystem never holds the ledger.
- Chrome extension: dashboard address and a passcode entered in the popup. It contains no broker key, Gateway key, page scraping, or Buy/Sell automation. Pending confirmation tokens and idempotency keys survive popup closure in Chrome session storage; passcodes are not stored.

No extra machine is needed. The paper accounts and existing recorder remain independent of the confirmed execution account.

## Order flow

1. The authenticated Vercel proposal handler requests a server review using only an exact ticker and quantity.
2. Python fetches signed account state, exchange status and broker data timestamps, reconciles previous orders, obtains the current REST book and fee schedules, and evaluates the existing CF/BRTI engine against the actual routed position.
3. Deterministic candidates must clear index/stream/quote/account freshness, warm-up and model gates, market identity, fee bounds, the current tick grid, spread, available top-level depth and timing checks. Entry needs estimated advantage after the reserved fee bound of at least 4 cents plus a 1-cent latency buffer by default. Exit needs net proceeds above the held-side settlement estimate by at least 0.5 cents plus that buffer. The exit quantity cannot exceed the actual routed position.
4. Jev chooses enter_yes, enter_no, exit, hold or skip from structured context. It cannot supply a ticker, price, quantity, account balance or risk limit. Python rejects decisions outside its current candidate set. Default minimum decision confidence is 0.7; that score is distinct from the engine's estimated settlement probability.
5. Python issues a random, hashed, expiring confirmation token bound to the exact intent, model decision and pause/resume generation. Default lifetime is 15 seconds. A proposal reserves no money and submits no order.
6. Confirmation re-fetches account and quote data and re-evaluates deterministic rules. The terms must still match exactly; changed prices require a new review. SQLite atomically consumes the proposal, enforces all caps and reserves the worst-case debit before any POST.
7. The adapter sends one signed V2 IOC request with a stable broker client_order_id. No order POST is automatically retried. Exits are reduce_only. The reservation transaction and broker dispatch both recheck freshness deadlines, including time waiting for the database or rate limiter.
8. Broker acknowledgments, individual fills, order status, positions and final market payouts drive reconciliation. Acknowledgment is different from a fill. IOC orders can fill partially. Unknown status keeps the reservation and pauses further submissions.

The API never accepts a client-supplied freshness flag or a replacement model probability. Operator and proposal credentials have separate roles. Explicit confirmation and authentication still matter when using the API without the extension.

## Budget semantics

All money is stored as integer units of 1e-8 dollars. Quantities are hundredths of a contract; requests accept exact fixed-point strings, not rounded floats.

| Cap | Enforcement |
| --- | --- |
| Cumulative purchase spend | Actual purchase fill cost plus fees, plus unresolved buy reservations. Selling or settling does not restore this budget. |
| Open exposure | Remaining position cost including entry fees, plus unresolved buy reservations. |
| Realized loss | Gross realized losses are accumulated; profitable trades do not erase them. Entry must also fit the worst-case loss of all open exposure. Exit fees and minimum proceeds must fit the remaining loss budget. |
| Per-order debit | Purchase-equivalent cost at the confirmed limit plus a conservative fee reserve, including for exits. |
| Quantity | Server maximum and available top-level depth; exits additionally require sufficient routed holdings. |

The supported account is a direct Kalshi member, where balance alignment is 0.0001 dollars. The fee reserve bounds a quadratic coefficient at most 0.07, rounding each possible 0.01-contract fill independently without relying on rebates. That is 0.0276 dollars per whole contract, rounded upward to the next 0.0001 dollars for fractional quantities. This uses the maximum quadratic fee over all possible improved execution prices. Series/event schedules above that bound, unsupported fee types, and fee changes scheduled within 60 seconds fail closed.

These caps apply to orders routed through this ledger. Separate manual trades or other bots are outside its spending controls. Use an isolated, initially empty subaccount where available. Position drift or resting account orders stop this execution path; the service will not adopt untracked positions or sell them automatically. A manual round trip with no net position change is outside the ledger's accounting.

Caps and account identity are bound into the database. Changing configured caps, key identity, environment, subaccount or owner host does not start a fresh budget: the existing ledger refuses the mismatch. There is no HTTP budget-reset endpoint. Never delete the database to resolve an unknown order.

## Supported hosting topology

Exactly one recorder/execution process on one explicitly named host, with a local durable volume, is supported. Existing Fly recorder storage at /data is the intended location. The ledger is /data/execution/ledger.sqlite3.

SQLite uses WAL, synchronous=FULL, foreign keys, independent connections and BEGIN IMMEDIATE for mutations. An exclusive process lock prevents two execution owners on the same database. Host identity must match EDGEBOOK_EXECUTION_HOST_ID, using FLY_MACHINE_ID on Fly or the local hostname otherwise. The configured durable mount must be a real mount and contain the ledger directory.

Do not use multiple Fly machines, independent replicated volumes, network SQLite filesystems, ephemeral disk, Vercel disk, or multiple execution processes. Do not create a replacement volume for a rolling deployment and enable execution on it. Multi-host coordination is unsupported and requires a different authoritative transactional datastore and fencing.

On restart the service pauses, clears account freshness, and converts unfinished submissions to unknown. It discovers orders by the stored client ID, checks exact intent and fill identities, and reconciles again before the user can resume. Absence from a broker list is never evidence that an uncertain order was rejected. Orders older than the broker's current read window may need operator investigation against historical records; the service retains the reservation while unresolved.

Reconciliation runs every five seconds and on status/control requests. Account lag, inconsistent fills, a missing response, position drift or an unsupported response pauses execution. Pause stops new authorizations; an already authorized/in-flight IOC can still fill. Autopilot and long-shot loops require explicit owner activation and remain subject to the durable ledger.

Back up SQLite with its backup API or a consistent volume snapshot, including WAL state as appropriate. Restore to the same owner/account binding, reconcile while paused, then explicitly resume. The service must never fail over by silently creating an empty ledger.

## Configuration remaining for the user

Use execution.env.example as a configuration reference, not as an activation script. Its enable flag is false.

On the existing recorder service:

- Keep the existing market-data credentials, dashboard token and /data volume.
- Configure separate execution credentials: EDGEBOOK_EXECUTION_KALSHI_KEY_ID and either EDGEBOOK_EXECUTION_KALSHI_PRIVATE_KEY or EDGEBOOK_EXECUTION_KALSHI_PRIVATE_KEY_PATH. Demo execution needs demo credentials; production needs production credentials.
- Set EDGEBOOK_EXECUTION_ENVIRONMENT to demo or production. Production market data remains the input engine's source; demo execution prices must match the demo broker book or the request is refused.
- Set EDGEBOOK_EXECUTION_TOPOLOGY=single_host_durable_sqlite, EDGEBOOK_EXECUTION_DURABLE_MOUNT=/data, and EDGEBOOK_EXECUTION_HOST_ID to the exact existing machine ID.
- Confirm direct membership with EDGEBOOK_EXECUTION_DIRECT_MEMBER=true, choose the subaccount, and set the five risk limits explicitly in cents/contracts.
- Generate two different random server secrets of at least 32 characters for EDGEBOOK_EXECUTION_API_TOKEN and EDGEBOOK_EXECUTION_PROPOSAL_TOKEN. They are not the dashboard publishing token.
- Enable EDGEBOOK_EXECUTION_ENABLED only when you choose to activate this path. The process still starts paused and requires reconciliation plus an explicit Resume action.

On Vercel:

| Variable | Value |
| --- | --- |
| FLY_DATA_URL | HTTPS origin of the existing recorder |
| DASHBOARD_ACCESS_PASSWORD | Strong ASCII passcode used by the dashboard and popup |
| EXECUTION_OPERATOR_TOKEN | Same as recorder EDGEBOOK_EXECUTION_API_TOKEN |
| EXECUTION_PROPOSAL_TOKEN | Same as recorder EDGEBOOK_EXECUTION_PROPOSAL_TOKEN |
| JEV_MODE | gateway |
| AI_GATEWAY_API_KEY | Server-only Vercel AI Gateway key |
| JEV_MODEL | typesafe-ai/jev, or a supported explicit model version |
| EXTENSION_ORIGIN | chrome-extension:// followed by the installed extension ID |

If JEV_MODEL changes, set EDGEBOOK_EXECUTION_JEV_MODEL to the identical identifier on the recorder. The audit records the configured identifier, the SDK's resolved model identifier and decision result. No fixture approval or alternate model fallback is used.

Existing dashboard data proxying still uses DASHBOARD_TOKEN; that token cannot authorize execution.

The legacy CLI production trade-once path is retired even when its old viability guard passes.

**Paper proof before real-money entries.** Review and confirmation of any new entry are refused until the paper account of the strategy whose prices execution uses (`EDGEBOOK_EXECUTION_PROOF_STRATEGY`, default `anchored`, which must be the engine's primary strategy) has at least `EDGEBOOK_EXECUTION_PROOF_MIN_TRADES` closed trades (default and minimum floor 200 / 100) across `EDGEBOOK_EXECUTION_PROOF_MIN_DAYS` separate UTC days (default 5, floor 3), with the 95% confidence interval of profit per contract entirely above zero. Exits of routed positions are never blocked. The owner may waive the gate with `EDGEBOOK_EXECUTION_REQUIRE_PAPER_PROOF=false`: entries are then allowed without paper proof, each one is audited as `paper_proof_waived` together with the current paper record, and `/execution/status` reports `paper_proof.required: false`. Per-order exact-terms confirmation and every money cap still apply. `/execution/status` reports the current proof record and the reason entries are blocked; every blocked attempt is audited. The long-shot pilot (below) checks the paper record of the `longshot` strategy instead, under the same thresholds and the same waiver.

## Long-shot pilot (real-money resting orders)

The pilot copies the paper long-shot seller with small real orders. Every `EDGEBOOK_EXECUTION_LONGSHOT_EVERY_S` seconds (default 5) it reads the paper strategy's latest calls and, using the same rule (`longshot_quote`) and the same data checks, works out which markets the paper strategy is selling a long shot in right now. In each of those markets it rests one post-only, good-till-canceled bid on the favourite side at the paper price, `EDGEBOOK_EXECUTION_LONGSHOT_CONTRACTS` contracts (default 1.00).

- **Off by default.** Switch it on in the popup ("Turn long-shot seller ON") or with `POST /execution/longshot {"confirmed": true, "enabled": true}`. It also needs Resume, like the autopilot. Turning it off, or Pause, cancels its resting orders.
- **Money caps.** Every resting order reserves its full worst-case debit (price × contracts + the fee reserve) under the same five caps before it is sent, and keeps it reserved until it is canceled or filled. The caps therefore still hold if every resting order fills and every position loses. A long-shot sale risks about 85-98¢ per contract to make a few cents, so the realized-loss cap is the number that matters.
- **Extra pilot limits.** At most `EDGEBOOK_EXECUTION_LONGSHOT_MAX_ORDERS` resting orders (default 5), and at most `EDGEBOOK_EXECUTION_LONGSHOT_EVENT_CENTS` (default 500 = $5) held plus reserved across all strikes of one close, since they move together. It never adds to a market it already holds and never sells: positions are held to the result, like the paper strategy.
- **Before each order** it refreshes the account, re-reads the Kalshi book, and refuses if the best bid on its side is no longer the paper price, the price is off the tick grid, the market is not active, or less than 30 s remain before its cut-off.
- **Cancels.** An order is canceled as soon as the paper strategy stops selling that market or changes price. Each order also carries `expiration_ts` = close − `EDGEBOOK_EXECUTION_LONGSHOT_EXPIRE_S` (default 300 s), so Kalshi removes it even if this host is down. A failed cancel pauses execution.
- **Reconciliation.** Account reads now return resting orders instead of refusing them. The ledger accepts only the resting orders it placed. A position may run ahead of the ledger by at most the unfilled remainder of our resting order on that side, because a fill can land between the order read and the account read. The next reconciliation applies it. Anything else still pauses.

**Verify on demo first.** The order fields `time_in_force: good_till_canceled`, `post_only` and `expiration_ts`, and the cancel call `DELETE /portfolio/events/orders/{order_id}`, follow Kalshi's documented V1/V2 conventions. They were checked only against offline fixtures, not against a live broker. Run the pilot with `EDGEBOOK_EXECUTION_ENVIRONMENT=demo` until an order rests, shows in Kalshi, cancels, and expires as expected. If Kalshi rejects a field, the order is refused (400) and nothing rests. If the cancel route is wrong, execution pauses and the order still expires at the cut-off.

## Build and load

From the repository root:

```sh
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
cd dashboard
npm ci --ignore-scripts
npm test
npm run check
```

The Manifest V3 extension is plain HTML/CSS/modules and needs no bundling. When ready, open chrome://extensions, enable Developer mode, choose Load unpacked, and select the repository's extension directory. Copy its ID into Vercel EXTENSION_ORIGIN. In the popup set the dashboard HTTPS origin, grant access to that origin, and enter the dashboard passcode.

Connect refreshes authoritative status. Resume requires its own checkbox and succeeds only after account reconciliation. Ask Jev creates a proposal or displays hold/skip. The order button remains disabled until the exact-terms checkbox is checked and the proposal is unexpired. A lost response must be followed by a status/reconciliation check; the stored idempotency key prevents resubmission of that same confirmation.

Publishing the code, changing service configuration, loading the extension and enabling real trading are user-operated steps. None was performed during implementation.

## API reference

All recorder routes require Bearer authentication and return no-store responses. POST routes require application/json. Operator and proposal tokens are never returned to the browser.

| Route | Method / role | Body |
| --- | --- | --- |
| /execution/status | GET / operator | none |
| /execution/review | POST / proposal | ticker, quantity |
| /execution/propose | POST / proposal | review_id, typed decision |
| /execution/confirm | POST / operator | proposal_id, confirmation_token, idempotency_key, exact intent, confirmed: true |
| /execution/pause | POST / operator | confirmed: true |
| /execution/resume | POST / operator | confirmed: true |
| /execution/reconcile | POST / operator | confirmed: true |

Browser routes are /api/jev-proposal and /api/execution?action=status|confirm|pause|resume|reconcile. The proxy does not expose the model-role endpoints directly. Browser attempts to supply model decisions, prices, limits or account freshness to Jev review are rejected.

## Evaluating incremental Jev value

The audit table retains review inputs, raw and blended engine forecasts, executable prices/depth, fee/cost bounds, allowed/rejected candidates and reasons, model decisions/confidence/latency, proposal and confirmation timestamps, actual fills and fees, realized outcomes, and final market payout observations even for reviewed skips. It does not store passwords, confirmation tokens, broker secrets or Gateway keys.

Join reviews, model_decision, proposal, confirmation_checks, reservation, broker_receipt, fill, settlement and market_outcome events by review_id, proposal_id, order key and ticker. All money within audit accounting is in 1e-8-dollar units; context quote prices use those units too. Compare against an executable-price baseline, not only directional accuracy or a midpoint forecast. Include rejections, partial fills, delays and the exact evaluation cutoff. Unfilled counterfactual trades are not realized performance.

No positive edge is established by these changes. Earlier held-out analysis favored the midpoint over the tested predictors and simulated trading lost after fees. Confidence thresholds are configuration, not measured calibration or profitability.

## Verification limits and references

Tests use synthetic fixture prices, ephemeral databases and offline broker/model mocks. They cover independent concurrent connections, duplicate requests, restart recovery, rejected/unknown orders, fractional partial fills, settlement, fee-inclusive limits, stale state, crafted-client rejection, role authentication, SDK response shape, extension controls and proxy behavior. Passing them does not establish broker compatibility with a particular funded account, production latency, model usefulness, Chrome installation behavior, or profitability.

Contracts checked against official documentation on 2026-10-17:

- [Kalshi V2 creation](https://docs.kalshi.com/api-reference/orders/create-order-v2), [order reads](https://docs.kalshi.com/api-reference/orders/get-order), [fills](https://docs.kalshi.com/api-reference/portfolio/get-fills), [positions](https://docs.kalshi.com/api-reference/portfolio/get-positions), [user data timestamp](https://docs.kalshi.com/api-reference/exchange/get-user-data-timestamp).
- [Kalshi direction conventions](https://docs.kalshi.com/getting_started/order_direction), [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding), [series fees](https://docs.kalshi.com/api-reference/market/get-series), [event fee changes](https://docs.kalshi.com/api-reference/events/get-event-fee-changes).
- [Vercel evaluation-to-decisions migration](https://vercel.com/i/ai-sdk-evaluation-to-decisions). The implementation pins ai 7.0.129, calls experimental_decide, and reads answers.action.choice plus its probability distribution. Version 6 from the scaffold did not establish that contract. The installed SDK's actual declarations and DecisionMockModelV4 are tested.
