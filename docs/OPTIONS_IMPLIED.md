# Options-implied odds vs Kalshi's hourly ladder

## Pre-registration (committed before any test close was evaluated)

Selected by `select_rule` on the earliest third of KXBTCD closes (before 2026-10-01 08:00 UTC,
`first_test_close = 1790409600`), from 36 combinations (vol source x edge threshold x role).
Every combination lost on train; this one lost least (−1.13¢/contract, 95% CI −3.77 to +0.90,
1,638 trades):

- vol source: `dvol` (Deribit DVOL, 30-day implied vol), driftless lognormal on Coinbase spot
- buy the side whose options probability exceeds the executable price by **>= 5¢ after the
  Kalshi taker fee**, at 60, 55, ..., 15 minutes before close; first firing per market only
- evaluated once on all later closes: `--rule dvol:0.05:taker`
