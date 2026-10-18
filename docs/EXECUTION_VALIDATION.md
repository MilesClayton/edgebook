# Execution validation record

Updated 2026-10-18. It supersedes the 2026-10-17 checkpoint written in the Codex worktree, which predates the fixes listed below.

## Automated checks (CI on every PR)

| Check | Result |
| --- | --- |
| Python suite | passes; 89.5% branch-aware coverage (floor 85%) |
| Ruff lint + format, strict mypy | pass (105 source/test files) |
| Dashboard / extension tests | 15 pass (`npm test`), TypeScript contract check passes (`npm run check`) |
| Dependency audits | pip-audit and `npm audit`: no known vulnerabilities |
| Docker image | built in CI (GitHub can pull `ghcr.io/astral-sh/uv`; the local GHCR denial in the Codex checkpoint does not affect CI or Fly deploys) |

## Fixed since the Codex checkpoint

- **Hourly tickers rejected:** the request pattern disallowed `.` (every hourly strike, e.g. `T81999.99`). Fixed and tested.
- **Exchange shards:** Kalshi runs several matching engines and every BTC market is on `exchange_index` 2. The broker only accepted shard 0. It is now configurable (`EDGEBOOK_EXECUTION_EXCHANGE_INDEX`, default 2) and used for market checks, orders, and balance/positions/orders/fills reads.
- **Paper-proof gate:** new real-money entries require a profitable paper record unless the owner waives it (`EDGEBOOK_EXECUTION_REQUIRE_PAPER_PROOF=false`). Waived entries are audited.
- **Engine starvation:** ticks took ~1 s on Fly once the long-shot seller ran, so the recorder's web server and health checks starved ("Failed to fetch"). Ticks are now ~10 ms (replay benchmark).
- **Extension:** remembers the dashboard password locally, auto-connects, auto-fills the current BTC 15-minute market, shows a panel on Kalshi market pages, and sends an order only on a single Buy click.
- **Dashboard:** a live-trading readiness panel shows warm-up progress and whether execution is paused or active, so a warm-up never looks like a trade signal.

## Verified in production

| Item | How | Result |
| --- | --- | --- |
| Kalshi production key | signed read-only `GET /portfolio/balance` | authenticated; cash is on the crypto shard (2) |
| Recorder market data | live WebSocket subscription | 637+ markets subscribed with the new key |
| Execution backend | `/execution/status` through the dashboard, as the extension | configured, enabled, production, paused until Resume |
| Dashboard access | requests without password / foreign origin | 401 / 403; extension origin allowed (CORS 204/200) |
| Vercel deployment | deployed files compared by hash with git | identical |

## Still to verify after each deploy

- A proposal-only Jev request after the engine's 15-minute warm-up (it cannot submit an order).
- The extension in Chrome after `git pull` in `~/Developer/python/edgebook` and a reload.

No strategy has a proven edge; real orders require the owner's Resume and a Buy click per order, within the configured caps.
