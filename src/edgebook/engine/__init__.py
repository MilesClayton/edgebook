"""Live decision engine: one call per market (BUY UP / BUY DOWN / NO TRADE,
or HOLD / SELL for an open position), recomputed every second from live data,
plus a paper account that trades those calls against real order-book depth."""
