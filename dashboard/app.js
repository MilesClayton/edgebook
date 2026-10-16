// edgebook dashboard. Page structure (sidebar router, metric cards, refresh
// control, Plotly panels) adapted from ryanfrigo/kalshi-ai-trading-bot
// scripts/trading_dashboard.py, MIT License; see THIRD_PARTY_LICENSES.md.
//
// Every number carries a plain-English hover description (title attribute).
"use strict";

const PAGES = { live: "Live Calls", paper: "Paper $500k", research: "Research", calibration: "Calibration", system: "System" };
const REFRESH_MS = { live: 3000, paper: 5000, system: 5000 };
let page = "live";
let timer = null;

// ---------------------------------------------------------------- data
async function load(kind) {
  try {
    const r = await fetch(`/api/data?kind=${kind}`, { cache: "no-store" });
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- helpers
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") n.className = v;
    else n.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return n;
}
const fmt = {
  usd: (x, d = 2) => (x == null ? "–" : `$${Number(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`),
  cents: (x) => (x == null ? "–" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}¢`),
  price: (x) => (x == null ? "–" : `${(x * 100).toFixed(0)}¢`),
  pct: (x) => (x == null ? "–" : `${(x * 100).toFixed(0)}%`),
  n: (x) => (x == null ? "–" : Number(x).toLocaleString(undefined, { maximumFractionDigits: 0 })),
  age: (ms) => (ms == null ? "–" : ms < 1500 ? `${(ms / 1000).toFixed(1)}s` : ms < 90_000 ? `${Math.round(ms / 1000)}s` : `${Math.round(ms / 60000)}m`),
  left: (s) => (s == null ? "–" : s >= 3600 ? `${(s / 3600).toFixed(1)}h` : s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.max(0, Math.round(s))}s`),
  time: (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }),
};
function card(label, value, sub, tip, tone = "") {
  return el("div", { class: `card ${tone}`, title: tip }, el("div", { class: "k" }, label), el("div", { class: "v" }, value), el("div", { class: "s" }, sub ?? ""));
}
function kv(rows) {
  const dl = el("dl", { class: "kv" });
  for (const [label, value, tip, tone] of rows) dl.append(el("dt", { title: tip }, label), el("dd", { class: tone ?? "" }, value));
  return dl;
}
function table(cols, rows, rowClass = () => "") {
  const head = el("tr", {}, cols.map((c) => el("th", { title: c.tip }, c.label)));
  const body = rows.map((r) => el("tr", { class: rowClass(r) || null }, cols.map((c) => {
    const v = c.get(r);
    const td = el("td", {}, c.fmt ? c.fmt(v) : v ?? "–");
    if (c.tone && typeof v === "number") td.className = v > 0 ? "pos" : v < 0 ? "neg" : "";
    return td;
  })));
  return el("div", { class: "panel" }, el("table", {}, el("thead", {}, head), el("tbody", {}, body)));
}
function empty(msg) {
  return el("div", { class: "empty" }, msg);
}
function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
const pendingCharts = [];
function plot(node, traces, layout = {}) {
  if (!node.isConnected) {
    pendingCharts.push(() => plot(node, traces, layout));
    return;
  }
  if (!window.Plotly) {
    node.replaceWith(empty("Charts are still loading; refresh in a moment."));
    return;
  }
  const axis = { gridcolor: css("--border"), zerolinecolor: css("--border") };
  Plotly.newPlot(node, traces, {
    margin: { l: 60, r: 16, t: 34, b: 40 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: css("--text"), size: 12 },
    xaxis: axis,
    yaxis: axis,
    legend: { orientation: "h", y: -0.2 },
    hovermode: "x unified",
    ...layout,
  }, { displayModeBar: false, responsive: true });
}

const TIP = {
  action: "The engine's call right now. BUY UP/DOWN only when its model thinks the contract is cheap by at least the minimum edge after fees and every data check passes. HOLD/SELL appear when the paper account owns this market.",
  fair: "Model's probability that UP wins, from the live CF Benchmarks index (the price Kalshi settles on), time left, and recent volatility. Inside the last 60 s it uses the part of the settlement average already printed.",
  upPrice: "What one UP (YES) contract costs right now: the lowest offer in Kalshi's order book. It pays $1 if UP wins.",
  downPrice: "What one DOWN (NO) contract costs right now. It pays $1 if DOWN wins.",
  edge: "Expected profit per contract if you buy now and hold to settlement: model chance − price − Kalshi's taker fee. Positive = cheap, negative = expensive.",
  index: "CF Benchmarks BRTI, the bitcoin index Kalshi uses to settle these markets, streamed live (about once a second).",
  strike: "The level the settlement average must reach. For 15-minute markets it's the 60-second average at the start of the window.",
  left: "Time until the market closes. Settlement is the average of the index over the final 60 seconds.",
  spread: "Gap between the best offer and the best bid for UP. Wide spreads cost you more to get in and out.",
  depth: "Contracts available at the best price right now. Bigger orders fill at worse prices.",
  vol: "How much the index moves per minute lately (in basis points; 1 bp = 0.01%). Higher volatility = less certainty.",
};

function questionFor(c) {
  const close = new Date((Date.now() / 1000 + c.seconds_left) * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (c.series === "KXBTC15M") return [`BTC up by ${close}?`, `15-min market · UP if the closing average ≥ the opening average (${fmt.usd(c.strike)})`];
  return [`BTC above ${fmt.usd(c.strike, 0)} at ${close}?`, "Hourly market · UP = above the strike at close"];
}
function badgeFor(action) {
  const cls = { "BUY UP": "up", "BUY DOWN": "down", HOLD: "hold", SELL: "sell" }[action] ?? "wait";
  return el("div", { class: `badge ${cls}`, title: TIP.action }, action);
}

// ---------------------------------------------------------------- pages
function closeMs(d, c) {
  return d.updated_ms + c.seconds_left * 1000;
}
function countdown(ms, cls = "countdown") {
  return el("span", { class: cls, "data-close": String(ms), title: TIP.left }, fmt.left((ms - Date.now()) / 1000));
}
function probBar(c) {
  const p = c.fair_up;
  if (p == null) return el("div", { class: "muted" }, "Model probability not available yet.");
  const up = el("div", { class: "up", style: `width:${(p * 100).toFixed(1)}%` }, p >= 0.12 ? `UP ${fmt.pct(p)}` : "");
  const down = el("div", { class: "down" }, 1 - p >= 0.12 ? `DOWN ${fmt.pct(1 - p)}` : "");
  const bar = el("div", { class: "probbar", title: "Coloured bar = the model's probability. White tick = what Kalshi's prices imply (mid of bid and ask). A gap between them is where an edge would come from." }, up, down);
  if (c.market_up != null) bar.append(el("div", { class: "mkt", style: `left:calc(${(c.market_up * 100).toFixed(1)}% - 1px)` }));
  return el("div", {}, bar, el("div", { class: "legend" }, el("span", {}, `Model: UP ${fmt.pct(p)}`), el("span", {}, `Kalshi prices imply: UP ${fmt.pct(c.market_up)}`)));
}
function planBox(c) {
  const cell = (k, v, tip) => el("div", { title: tip }, el("div", { class: "k" }, k), el("div", { class: "v" }, v));
  return el("div", { class: "plan" },
    cell("Buy UP at or below", c.buy_up_below == null ? "never" : fmt.price(c.buy_up_below), "The highest price for UP that still leaves the minimum expected profit after Kalshi's fee. Above this, UP is not a buy."),
    cell("Buy DOWN at or below", c.buy_down_below == null ? "never" : fmt.price(c.buy_down_below), "The highest price for DOWN that still leaves the minimum expected profit after fees."),
    cell("Own UP? Sell when bid ≥", c.sell_up_at == null ? "hold" : fmt.price(c.sell_up_at), "If someone will pay this much for your UP contract, selling beats holding to the end (after the fee). Below it, holding is worth more on average."),
    cell("Own DOWN? Sell when bid ≥", c.sell_down_at == null ? "hold" : fmt.price(c.sell_down_at), "Same rule for a DOWN contract."),
  );
}
function strikeLabel(c) {
  return c.series === "KXBTC15M" ? `15-min · start ${fmt.usd(c.strike)}` : fmt.usd(c.strike, 0);
}

function nowPanel(d, held) {
  const buys = d.calls.filter((c) => c.action.startsWith("BUY"));
  const exits = d.calls.filter((c) => c.action === "SELL");
  const best = (c) => Math.max(c.edge_up ?? -9, c.edge_down ?? -9);
  if (buys.length || exits.length) {
    const c = exits[0] ?? buys.sort((a, b) => best(b) - best(a))[0];
    const [q] = questionFor(c);
    const side = c.action === "BUY UP" || (c.action === "SELL" && c.position?.side === "yes") ? "UP" : "DOWN";
    const limit = side === "UP" ? c.buy_up_below : c.buy_down_below;
    const sellAt = side === "UP" ? c.sell_up_at : c.sell_down_at;
    return el("div", { class: "now go" },
      el("div", { class: "eyebrow" }, "Right now"),
      el("div", { class: "now-title" }, `${c.action} · ${q}`),
      el("div", {}, c.reason),
      el("div", { class: "now-sub" }, c.action === "SELL" ? `Sell at ${fmt.price(side === "UP" ? c.up_sell : c.down_sell)} or better.` : `Pay no more than ${fmt.price(limit)}. Afterwards: sell if the bid reaches ${fmt.price(sellAt)}, otherwise hold to the result. Closes in `, c.action === "SELL" ? "" : countdown(closeMs(d, c), "")),
      buys.length + exits.length > 1 ? el("div", { class: "now-sub" }, `${buys.length + exits.length - 1} more market(s) also have a call; see below.`) : null,
    );
  }
  const ranked = d.calls.filter((c) => c.edge_up != null).sort((a, b) => best(b) - best(a));
  const top = ranked[0];
  const waiting = d.calls.find((c) => c.reason.startsWith("Waiting"));
  const title = waiting && !top ? "Not ready" : "NO TRADE right now";
  const sub = top
    ? `Kalshi's prices are fair. Closest: ${questionFor(top)[0]} at ${fmt.cents(best(top))} expected profit (needs ${fmt.cents(d.config.min_edge)}). It becomes a buy if UP drops to ${fmt.price(top.buy_up_below)} or DOWN to ${fmt.price(top.buy_down_below)}.`
    : waiting ? waiting.reason : "No open markets.";
  return el("div", { class: "now" },
    el("div", { class: "eyebrow" }, "Right now"),
    el("div", { class: "now-title" }, title),
    el("div", { class: "now-sub" }, sub),
    held.size ? el("div", { class: "now-sub" }, `The paper account is holding ${held.size} position(s); see their HOLD/SELL calls below.`) : null,
  );
}

function fifteenPanel(d, c, held) {
  const [q, sub] = questionFor(c);
  const h = held.get(c.ticker);
  const gates = Object.entries(c.gates ?? {});
  const left = el("div", { class: "call" },
    el("div", { class: "call-head" }, el("div", {}, el("div", { class: "call-q" }, q), el("div", { class: "call-sub" }, sub)), countdown(closeMs(d, c))),
    badgeFor(c.action),
    el("div", { class: "reason" }, c.reason),
    probBar(c),
    planBox(c),
    kv([
      ["Buy UP now costs", fmt.price(c.up_price), TIP.upPrice],
      ["Buy DOWN now costs", fmt.price(c.down_price), TIP.downPrice],
      ["Expected profit UP / DOWN", `${fmt.cents(c.edge_up)} / ${fmt.cents(c.edge_down)}`, TIP.edge],
      ["BTC vs start level", c.index && c.strike ? `${c.index >= c.strike ? "+" : ""}${fmt.usd(c.index - c.strike, 0)} (${c.z_score == null ? "–" : c.z_score.toFixed(1)}σ)` : "–", "How far the settlement index is above (+) or below (−) the level it must beat, and how many typical moves (σ) that is for the time left. Beyond about ±2σ the outcome is rarely in doubt."],
      ["Spread · depth", `${fmt.price(c.spread)} · ${fmt.n(c.depth_up)} UP / ${fmt.n(c.depth_down)} DOWN`, `${TIP.spread} ${TIP.depth}`],
      ["Volatility", c.sigma_per_min_bps ? `${c.sigma_per_min_bps.toFixed(1)} bp/min` : "warming up", TIP.vol],
    ]),
    h ? el("div", { class: "position", title: "The simulated $500k account's position in this market." }, `Paper holds ${fmt.n(h.contracts)} ${h.side} @ ${fmt.price(h.avg_cost)} · unrealized ${h.unrealized == null ? "–" : fmt.usd(h.unrealized, 0)}`) : null,
    el("div", { class: "gates" }, gates.map(([name, ok]) => el("span", { class: `gate ${ok ? "ok" : "no"}`, title: ok ? `Check passed: ${name}` : `Blocking new entries: ${name}` }, `${ok ? "✓" : "✗"} ${name}`))),
  );
  const chart = el("div", { class: "panel chart" });
  const right = el("div", {}, el("div", { class: "eyebrow", title: TIP.index }, "Settlement index this window vs the level UP must beat"), chart);
  const openMs = closeMs(d, c) - 900_000;
  const pts = (d.index_series ?? []).filter((p) => p[0] >= openMs - 60_000);
  if (pts.length < 3) chart.replaceChildren(empty("Not enough index data for this window yet."));
  else {
    plot(chart, [
      { x: pts.map((p) => new Date(p[0])), y: pts.map((p) => p[1]), type: "scatter", mode: "lines", name: "BTC index", line: { color: css("--accent"), width: 2 }, hovertemplate: "$%{y:,.2f}<extra>index</extra>" },
      { x: [new Date(openMs), new Date(closeMs(d, c))], y: [c.strike, c.strike], mode: "lines", name: "UP needs ≥ this", line: { dash: "dot", color: css("--warn"), width: 2 }, hovertemplate: "start level $%{y:,.2f}<extra></extra>" },
    ], { xaxis: { range: [new Date(openMs), new Date(closeMs(d, c))], gridcolor: css("--border") }, yaxis: { gridcolor: css("--border"), tickprefix: "$", tickformat: ",.0f" }, shapes: [{ type: "rect", xref: "x", yref: "paper", x0: new Date(closeMs(d, c) - 60_000), x1: new Date(closeMs(d, c)), y0: 0, y1: 1, fillcolor: "rgba(255,212,122,.10)", line: { width: 0 } }], annotations: [{ x: new Date(closeMs(d, c) - 30_000), y: 1, yref: "paper", text: "settlement average", showarrow: false, font: { size: 10, color: css("--warn") } }] });
  }
  return el("div", { class: "two" }, left, right);
}

function ladder(d, hourly) {
  const close = hourly.length ? closeMs(d, hourly[0]) : null;
  const rows = hourly.slice().sort((a, b) => b.strike - a.strike);
  const t = table([
    { label: "BTC above", get: (c) => fmt.usd(c.strike, 0), tip: "The strike: UP wins if the 60-second settlement average at the close is above this." },
    { label: "Model UP", get: (c) => c.fair_up, fmt: fmt.pct, tip: TIP.fair },
    { label: "Kalshi UP", get: (c) => c.market_up, fmt: fmt.pct, tip: "Probability implied by Kalshi's prices (mid of best bid and ask)." },
    { label: "UP costs", get: (c) => c.up_price, fmt: fmt.price, tip: TIP.upPrice },
    { label: "Buy UP ≤", get: (c) => c.buy_up_below, fmt: (v) => (v == null ? "never" : fmt.price(v)), tip: "UP is a buy at or below this price." },
    { label: "DOWN costs", get: (c) => c.down_price, fmt: fmt.price, tip: TIP.downPrice },
    { label: "Buy DOWN ≤", get: (c) => c.buy_down_below, fmt: (v) => (v == null ? "never" : fmt.price(v)), tip: "DOWN is a buy at or below this price." },
    { label: "Best profit", get: (c) => Math.max(c.edge_up ?? -9, c.edge_down ?? -9), fmt: (v) => (v < -1 ? "–" : fmt.cents(v)), tone: true, tip: TIP.edge },
    { label: "Call", get: (c) => c.action, tip: TIP.action },
  ], rows, (c) => (c.action !== "NO TRADE" ? "go" : ""));
  return el("div", {}, el("h2", {}, "Hourly markets · BTC above a price at ", close ? new Date(close).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "–", " · ", close ? countdown(close, "") : ""), t);
}

function signalLog(d) {
  const log = (d.signal_log ?? []).slice().reverse();
  return el("div", {},
    el("h2", { title: "Every time the engine's call for a market changed. Use it to see when buys appeared and how long they lasted." }, "Signal log"),
    log.length ? table([
      { label: "Time", get: (e) => fmt.time(e.ts_ms) },
      { label: "Market", get: (e) => (e.series === "KXBTC15M" ? `15-min ${fmt.usd(e.strike, 0)}` : `above ${fmt.usd(e.strike, 0)}`) },
      { label: "Change", get: (e) => `${e.from ?? "–"} → ${e.to}` },
      { label: "Model UP", get: (e) => e.fair_up, fmt: fmt.pct },
      { label: "UP / DOWN cost", get: (e) => `${fmt.price(e.up_price)} / ${fmt.price(e.down_price)}` },
      { label: "Why", get: (e) => e.reason },
    ], log) : empty("No call changes yet since the engine started. Quiet markets stay on NO TRADE."));
}

function helpBox(d) {
  return el("details", { class: "help" },
    el("summary", {}, "How to use this page"),
    el("ul", {},
      el("li", {}, "Start at “Right now”. It names the single best action, or says why there is none."),
      el("li", {}, `BUY UP / BUY DOWN appears only when the model's expected profit is at least ${fmt.cents(d.config.min_edge)} per contract after Kalshi's fee and every data check is green. Pay no more than the “Buy … at or below” price.`),
      el("li", {}, "After buying: sell if the bid reaches the “Sell when bid ≥” price (selling then beats holding). Otherwise hold to the result. The paper account follows exactly these rules."),
      el("li", {}, "Gray NO TRADE means Kalshi's price is fair or a check failed (stale data, wide spread, too close to the close). That is the right answer most of the time."),
      el("li", {}, "Hover any label or number for what it means. Nothing here is guaranteed; past tests found no reliable edge, so watch the Paper $500k results before trusting it with real money."),
    ));
}

async function live(root) {
  const [lv, paper] = await Promise.all([load("live"), load("paper")]);
  const d = lv?.data;
  if (!d) return root.append(empty("No live data yet. The engine on Fly publishes every 2 seconds once it starts."));
  const ageNow = Date.now() - d.updated_ms;
  const stale = ageNow > 15_000;
  const held = new Map((paper?.data?.holdings ?? []).map((h) => [h.ticker, h]));
  const gap = d.index && d.exchanges_mid ? (d.exchanges_mid / d.index - 1) * 1e4 : null;
  root.append(el("div", { class: "statusbar" },
    el("span", { class: `pill ${stale ? "bad" : "good"}`, title: "How old this page's data is right now. Refreshes every 3 seconds." }, stale ? `STALE ${fmt.age(ageNow)}` : `LIVE · ${fmt.age(ageNow)}`),
    el("span", { class: `pill ${d.index_age_ms <= 3000 ? "good" : "bad"}`, title: TIP.index }, `BTC index ${fmt.usd(d.index)} · ${fmt.age(d.index_age_ms)}`),
    el("span", { class: `pill ${d.ws_age_ms <= 10_000 ? "good" : "bad"}`, title: "Time since the last message from Kalshi's live order-book stream." }, `Kalshi ${fmt.age(d.ws_age_ms)}`),
    el("span", { class: `pill ${gap == null ? "warn" : Math.abs(gap) <= d.config.max_spot_index_gap_bps ? "good" : "bad"}`, title: "Coinbase/Kraken/Bitstamp vs the settlement index. A big gap means spot is moving faster than the index: the engine waits so you don't buy on a lag." }, gap == null ? "exchanges –" : `exchanges ${gap >= 0 ? "+" : ""}${gap.toFixed(1)} bp`),
  ));
  if (stale) root.append(el("div", { class: "banner" }, `Live feed is ${fmt.age(ageNow)} old. The engine may be restarting; calls are NOT current.`));
  root.append(nowPanel(d, held));
  const fifteen = d.calls.filter((c) => c.series === "KXBTC15M");
  const hourly = d.calls.filter((c) => c.series !== "KXBTC15M");
  root.append(el("h2", {}, "15-minute market"));
  root.append(fifteen.length ? fifteenPanel(d, fifteen[0], held) : empty("No 15-minute market is open right now; the next one opens on the quarter hour."));
  root.append(hourly.length ? ladder(d, hourly) : empty("No hourly markets within the next hour."));
  root.append(signalLog(d), helpBox(d));
}

async function paper(root) {
  const p = (await load("paper"))?.data;
  if (!p) return root.append(empty("The paper account hasn't published yet."));
  root.append(el("div", { class: "banner" }, "Simulated account: it follows the Live Calls automatically against real Kalshi order books and fees. No real money is involved."));
  const ret = p.return_pct;
  root.append(el("div", { class: "cards" },
    card("Equity", fmt.usd(p.equity, 0), `started at ${fmt.usd(p.starting_cash, 0)}`, "Cash plus open positions valued at what selling them would pay right now (best bid minus fee).", ret >= 0 ? "good" : "bad"),
    card("Return", `${ret >= 0 ? "+" : ""}${ret.toFixed(2)}%`, `since ${new Date(p.started_ms).toLocaleDateString()}`, "Equity versus the $500,000 starting balance."),
    card("Realized P&L", fmt.usd(p.realized, 0), `${fmt.n(p.closed_trades)} closed trades`, "Profit or loss locked in by selling or by markets settling."),
    card("Unrealized P&L", fmt.usd(p.unrealized, 0), `${p.holdings.length} open positions`, "Paper profit/loss on positions still open, at current sell prices."),
    card("Win rate", p.win_rate == null ? "–" : fmt.pct(p.win_rate), "closed trades in profit", "Share of closed trades that made money. Not the goal by itself: a strategy can win often and still lose money."),
    card("Status", p.halted ? "PAUSED" : "Trading", p.halted ? "daily loss limit hit" : `risk ${(p.risk.max_risk_per_trade * 100).toFixed(0)}%/trade, ≤ ${fmt.usd(p.risk.max_per_market, 0)}/market`, "Entries pause for the rest of the UTC day if the account drops 5% from its start-of-day value.", p.halted ? "bad" : "good"),
  ));
  const eq = p.equity_series ?? [];
  if (eq.length > 1) {
    const chart = el("div", { class: "panel chart" });
    root.append(el("h2", { title: "Account value over time, sampled every 10 seconds." }, "Equity"), chart);
    plot(chart, [{ x: eq.map((x) => new Date(x[0])), y: eq.map((x) => x[1]), type: "scatter", mode: "lines", name: "equity", line: { color: css("--accent-2"), width: 2 }, hovertemplate: "$%{y:,.0f}<extra></extra>" }], { yaxis: { gridcolor: css("--border"), tickprefix: "$", tickformat: ",.0f" } });
  }
  root.append(el("h2", {}, "Open positions"));
  root.append(p.holdings.length ? table([
    { label: "Market", get: (h) => h.ticker },
    { label: "Side", get: (h) => h.side, tip: "UP = YES, DOWN = NO" },
    { label: "Contracts", get: (h) => h.contracts, fmt: fmt.n },
    { label: "Avg cost", get: (h) => h.avg_cost, fmt: fmt.price, tip: "Average price paid per contract including fees" },
    { label: "Sell value now", get: (h) => h.mark, fmt: fmt.price, tip: "Best bid minus fee: what selling one contract pays now" },
    { label: "Unrealized", get: (h) => h.unrealized, fmt: (v) => fmt.usd(v, 0), tone: true },
    { label: "Status", get: (h) => (h.awaiting_result ? "closed: waiting for Kalshi result" : "live") },
  ], p.holdings) : empty("No open positions."));
  root.append(el("h2", {}, "Trade log (newest first)"));
  const fills = (p.fills ?? []).slice().reverse();
  root.append(fills.length ? table([
    { label: "Time", get: (f) => fmt.time(f.ts_ms) },
    { label: "Action", get: (f) => f.action },
    { label: "Market", get: (f) => f.ticker },
    { label: "Side", get: (f) => (f.side === "yes" ? "UP" : "DOWN") },
    { label: "Contracts", get: (f) => f.contracts, fmt: fmt.n },
    { label: "Price", get: (f) => f.price, fmt: fmt.price, tip: "Average fill price per contract, before fees (settlement = $1 or $0)" },
    { label: "Fees", get: (f) => f.fees, fmt: (v) => fmt.usd(v, 0) },
    { label: "P&L", get: (f) => f.pnl, fmt: (v) => (v == null ? "–" : fmt.usd(v, 0)), tone: true },
    { label: "Why", get: (f) => f.reason },
  ], fills) : empty("No trades yet. The engine only buys when a market is mispriced by at least the minimum edge; on most minutes it correctly waits."));
}

async function research(root) {
  const r = (await load("research"))?.data;
  root.append(el("div", { class: "banner" }, "Bottom line from 30 days of past markets: no strategy tested so far makes money after fees. Kalshi's own prices are usually as accurate as our model. Treat every call as unproven until the paper account shows profit over hundreds of trades."));
  const series = r?.series ?? {};
  if (!Object.keys(series).length) return root.append(empty("No research report published yet."));
  const rows = [];
  for (const [s, v] of Object.entries(series)) for (const st of v.strategies) rows.push({ s, name: st.name, all: st.all_oos, hold: st.holdout });
  root.append(table([
    { label: "Market", get: (x) => (x.s === "KXBTC15M" ? "15-minute" : "Hourly") },
    { label: "Strategy", get: (x) => x.name, tip: "market = Kalshi's own price; model_t4 = our fair value; blend/logreg/gbm = learned combinations" },
    { label: "Accuracy (Brier)", get: (x) => x.hold.brier, fmt: (v) => (v == null ? "–" : v.toFixed(4)), tip: "Average squared error of the probabilities on the last 7 days. Lower is better." },
    { label: "Kalshi's accuracy", get: (x) => x.hold.market_brier, fmt: (v) => (v == null ? "–" : v.toFixed(4)), tip: "The same score for Kalshi's own prices. To have an edge you must beat this." },
    { label: "Trades", get: (x) => x.hold.taker["0.02"]?.n, fmt: fmt.n, tip: "Trades taken on the last 7 days when expected edge > 2¢" },
    { label: "Avg profit / contract", get: (x) => x.hold.taker["0.02"]?.mean, fmt: fmt.cents, tone: true, tip: "Average result per contract after fees on those trades" },
    { label: "Verdict", get: (x) => { const t = x.hold.taker["0.02"]; if (!t || !t.n) return "no trades"; return t.ci_lo > 0 ? "profitable" : t.ci_hi < 0 ? "loses money" : "unproven (could be luck)"; }, tip: "Based on the 95% confidence interval of the average profit" },
  ], rows));
}

async function calibration(root) {
  const cal = (await load("calibration"))?.data;
  const hs = cal?.horizons ?? [];
  if (!hs.length) return root.append(empty("No calibration report published yet."));
  root.append(el("p", { class: "muted" }, "If the model says 70%, UP should win about 70% of the time. Points on the dotted line = honest probabilities. This is checked on past settled markets."));
  const pick = el("select", { title: "Which forecast horizon and model to plot" }, hs.flatMap((h) => Object.keys(h.models).map((m) => el("option", { value: `${h.horizon_min}|${m}` }, `${h.horizon_min} min before close · ${m}`))));
  const chart = el("div", { class: "panel chart" });
  root.append(pick, chart);
  const draw = () => {
    const [hm, m] = pick.value.split("|");
    const b = hs.find((h) => String(h.horizon_min) === hm).models[m].buckets.filter((x) => x.n && x.mean_p != null && x.freq != null);
    if (!b.length) return;
    plot(chart, [
      { x: [0, 1], y: [0, 1], mode: "lines", line: { dash: "dot", color: css("--muted") }, name: "perfectly honest", hoverinfo: "skip" },
      { x: b.map((x) => x.mean_p), y: b.map((x) => x.freq), mode: "markers+lines", name: m, error_y: { type: "data", symmetric: false, array: b.map((x) => x.ci_hi - x.freq), arrayminus: b.map((x) => x.freq - x.ci_lo) }, customdata: b.map((x) => x.n), hovertemplate: "said %{x:.0%} → happened %{y:.0%} (n=%{customdata})<extra></extra>" },
    ], { hovermode: "closest", xaxis: { title: "model said P(UP)", range: [0, 1], gridcolor: css("--border"), tickformat: ".0%" }, yaxis: { title: "actually happened", range: [0, 1], gridcolor: css("--border"), tickformat: ".0%" } });
  };
  pick.addEventListener("change", draw);
  draw();
}

async function system(root) {
  const [lv, snap] = await Promise.all([load("live"), load("snapshot")]);
  const d = lv?.data;
  const rec = snap?.data?.recorder ?? {};
  root.append(el("div", { class: "cards" },
    card("Live engine", d ? fmt.age(Date.now() - d.updated_ms) : "offline", "since last publish", "The engine re-decides every second and publishes every 2 seconds.", d && Date.now() - d.updated_ms < 15_000 ? "good" : "bad"),
    card("Settlement index age", fmt.age(d?.index_age_ms), "BRTI via Kalshi", TIP.index, d?.index_age_ms <= 3000 ? "good" : "bad"),
    card("Kalshi stream age", fmt.age(d?.ws_age_ms), "order books + trades", "Time since the last message on Kalshi's live connection.", d?.ws_age_ms <= 10_000 ? "good" : "bad"),
    card("Recorder", rec.msgs_per_s != null ? `${Math.round(rec.msgs_per_s)} msg/s` : "–", `${fmt.n(rec.markets)} markets · ${fmt.n(rec.gaps)} gaps`, "Raw data saved for research. Gaps = missed messages (should be 0)."),
    card("Disk", rec.disk_total_gb ? `${((rec.disk_used_gb / rec.disk_total_gb) * 100).toFixed(0)}%` : "–", rec.disk_total_gb ? `${rec.disk_used_gb.toFixed(1)} of ${rec.disk_total_gb.toFixed(0)} GB` : "", "Recorder volume on Fly."),
  ));
  const ex = Object.entries(d?.exchanges ?? {});
  root.append(el("h2", {}, "Exchange feeds"));
  root.append(ex.length ? table([
    { label: "Exchange", get: (x) => x[0] },
    { label: "Active", get: (x) => (x[1].active ? "yes" : "no") },
    { label: "Quote age", get: (x) => x[1].staleness_ms, fmt: fmt.age, tip: "How old that exchange's best bid/offer is" },
    { label: "Latency", get: (x) => x[1].latency_ms, fmt: (v) => (v == null ? "–" : `${v} ms`), tip: "Exchange timestamp to our receive time" },
  ], ex) : empty("Exchange feeds not connected yet."));
}

const RENDER = { live, paper, research, calibration, system };

async function render() {
  const root = document.getElementById("content");
  document.getElementById("page-title").textContent = PAGES[page];
  const frag = el("div");
  try {
    await RENDER[page](frag);
  } catch (e) {
    frag.replaceChildren(empty(`This page hit an error: ${e.message}`));
  }
  root.replaceChildren(...frag.childNodes);
  for (const job of pendingCharts.splice(0)) job();
  document.getElementById("updated").textContent = `updated ${fmt.time(Date.now())}`;
  clearTimeout(timer);
  if (REFRESH_MS[page]) timer = setTimeout(render, REFRESH_MS[page]);
  sideStatus();
}

async function sideStatus() {
  const lv = await load("live");
  const age = lv?.data ? Date.now() - lv.data.updated_ms : null;
  document.getElementById("side-status").replaceChildren(
    el("div", { title: "Age of the live engine's latest publish" }, el("span", { class: `pill ${age != null && age < 15_000 ? "good" : "bad"}` }, "engine"), " ", fmt.age(age)),
    el("div", { title: TIP.index }, el("span", { class: `pill ${lv?.data?.index_age_ms <= 3000 ? "good" : "bad"}` }, "index"), " ", fmt.age(lv?.data?.index_age_ms)),
  );
}

document.getElementById("nav").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-page]");
  if (!a) return;
  page = a.dataset.page;
  for (const x of document.querySelectorAll("#nav a")) x.classList.toggle("active", x === a);
  render();
});
document.getElementById("refresh").addEventListener("click", render);
render();
// live countdowns tick every second between data refreshes
setInterval(() => {
  for (const n of document.querySelectorAll("[data-close]")) n.textContent = fmt.left((Number(n.dataset.close) - Date.now()) / 1000);
}, 1000);
