// Pure helpers for the Kalshi page panel: which market the page shows, and which of
// Edgebook's live calls belong to it. Loaded before kalshi.js as a classic content script
// (content scripts cannot be ES modules), so it publishes one global.
"use strict";
(function () {
  // KXBTC15M-26OCT161345-45 (15-min) and KXBTCD-26OCT1607-T81999.99 (hourly strike)
  const TICKER = /(kxbtc15m|kxbtcd)-(\d{2}[a-z]{3}\d{4,6})(?:-(t\d+(?:\.\d+)?|\d{2}))?(?![a-z0-9])/;
  const SERIES = /\/markets\/(kxbtc15m|kxbtcd)(?:\/|$)/;

  /** The series, event and (if the URL names one strike) market a kalshi.com URL shows. */
  function locate(href) {
    let url;
    try {
      url = new URL(href);
    } catch {
      return null;
    }
    if (url.hostname !== "kalshi.com" && !url.hostname.endsWith(".kalshi.com")) return null;
    const path = url.pathname.toLowerCase();
    // Kalshi Pro (pro.kalshi.com) may name the market in the query or hash instead of the path
    let where = path;
    try {
      where = `${path} ${decodeURIComponent(url.search + url.hash).toLowerCase()}`;
    } catch {
      // malformed escape: the path alone
    }
    const m = TICKER.exec(where);
    if (m) {
      const series = m[1].toUpperCase();
      const event = `${series}-${m[2].toUpperCase()}`;
      return { series, event, ticker: m[3] ? `${event}-${m[3].toUpperCase()}` : null };
    }
    const s = SERIES.exec(path) || /(?:^|[^a-z0-9])(kxbtc15m|kxbtcd)(?![a-z0-9-])/.exec(where);
    if (s) return { series: s[1].toUpperCase(), event: null, ticker: null };
    // Pro's multi-market canvas: show the market autopilot trades
    return url.hostname === "pro.kalshi.com"
      ? { series: "KXBTC15M", event: null, ticker: null, pro: true }
      : null;
  }

  function belongs(ticker, series, ref) {
    if (ref.ticker) return ticker === ref.ticker;
    if (ref.event) return ticker.startsWith(`${ref.event}-`);
    return series === ref.series || ticker.startsWith(`${ref.series}-`);
  }

  /** Edgebook's calls for the page's market(s), highest strike first. */
  function callsFor(live, ref) {
    const calls = (live && live.calls) || [];
    return calls
      .filter((c) => belongs(c.ticker, c.series, ref))
      .sort((a, b) => (b.strike || 0) - (a.strike || 0));
  }

  /** The long-shot seller's resting orders on the page's market(s). */
  function longshotsFor(live, ref) {
    return ((live && live.longshot_orders) || []).filter((o) => belongs(o.ticker, null, ref));
  }

  const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
  const price = (x) => (x == null ? "–" : `${Math.round(x * 100)}¢`);
  const cents = (x) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}¢`);

  /** One display line per call; plain text only (rendered with textContent). */
  function describe(call) {
    const label = call.series === "KXBTC15M"
      ? `15-min · start $${Math.round(call.strike || 0).toLocaleString("en-US")}`
      : `Above $${Math.round((call.strike || 0) + 0.01).toLocaleString("en-US")}`;
    const best = Math.max(call.edge_up ?? -9, call.edge_down ?? -9);
    return {
      ticker: call.ticker,
      label,
      action: call.reason && call.reason.startsWith("Waiting") ? "WAITING" : call.action,
      odds: `Model ${pct(call.model_up)} · Kalshi ${pct(call.market_up)} · Fair ${pct(call.fair_up)}`,
      plan: `UP ${price(call.up_price)} (buy ≤ ${price(call.buy_up_below)}) · DOWN ${price(call.down_price)} (buy ≤ ${price(call.buy_down_below)})`,
      edge: best < -1 ? "–" : cents(best),
      reason: call.reason || "",
    };
  }

  function executionState(execution, checkedMs, now = Date.now()) {
    if (!execution || !Number.isFinite(checkedMs) || now - checkedMs > 15000) {
      return { label: "CHECKING", title: "Trading status unavailable", detail: "Reconnect in Controls to verify whether trading is running.", tone: "warn" };
    }
    if (!execution.configured || !execution.enabled) return {
      label: "UNAVAILABLE", title: "Live trading unavailable",
      detail: execution.reason || execution.error || "Open Controls to check the connection.", tone: "bad",
    };
    if (execution.unknown_or_pending) return { label: "CHECK ORDER", title: "An order needs reconciliation", detail: "New orders are blocked until its outcome is confirmed.", tone: "warn" };
    if (execution.paused && execution.auto_recovery) return {
      label: "RECOVERING", title: "Waiting for Kalshi to recover",
      detail: `Retrying every 5 seconds · ${execution.recovery_checks || 0}/3 clean checks. Your existing run resumes automatically after reconciliation.`, tone: "warn",
    };
    if (execution.paused) return { label: "STOPPED", title: "Trading is paused", detail: execution.error || "Save a budget and press Start in Controls when ready.", tone: "warn" };
    if (execution.autopilot || execution.longshot) return {
      label: "AUTOMATIC", title: execution.autopilot && execution.longshot ? "Jev + long-shot are running" : execution.autopilot ? "Jev is watching your markets" : "Long-shot bids are running",
      detail: "Real orders run automatically within your saved budget. Manage trading in Controls.", tone: "good",
    };
    return { label: "MANUAL", title: "Automatic trading is off", detail: "Manual proposals still need your Buy confirmation in Controls.", tone: "warn" };
  }

  globalThis.EdgebookKalshi = { locate, callsFor, longshotsFor, describe, executionState, pct, price, cents };
})();
