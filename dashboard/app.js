// edgebook dashboard. Page structure (sidebar router, metric cards, refresh
// control, Plotly panels) adapted from ryanfrigo/kalshi-ai-trading-bot
// scripts/trading_dashboard.py, MIT License; see THIRD_PARTY_LICENSES.md.
//
// Every number carries a plain-English hover description (title attribute).
"use strict";

const PAGES = { live: "Live Calls", paper: "Paper $500k", research: "Research", calibration: "Calibration", system: "System" };
const REFRESH_MS = { live: 3000, paper: 5000, system: 5000 };
let page = PAGES[location.hash.slice(1)] ? location.hash.slice(1) : "live";
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
  usd: (x, d = 2) => (x == null ? "–" : `${x < 0 ? "−" : ""}$${Math.abs(Number(x)).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`),
  cents: (x) => (x == null ? "–" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}¢`),
  price: (x) => (x == null ? "–" : Math.abs(x * 100 - Math.round(x * 100)) > 1e-6 ? `${(x * 100).toFixed(1)}¢` : `${Math.round(x * 100)}¢`),
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
    const td = el("td", { class: c.cls ?? null }, c.fmt ? c.fmt(v) : v ?? "–");
    if (c.tone && typeof v === "number") td.classList.add(v > 0 ? "pos" : v < 0 ? "neg" : "flat");
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
// ---------------------------------------------------------------- SVG charts
// Dependency-free charts (no CDN to wait for). Hover any point for its value.
const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, ...kids) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, String(v));
  for (const k of kids) if (k != null) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return n;
}
function niceTicks(lo, hi, n = 4) {
  if (!(hi > lo)) return [lo];
  const step0 = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= step0) ?? step0;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

// series: [{name, points: [[x, y], ...], color, dash}]; x is ms (time) unless opts.xNumeric
function lineChart(series, opts = {}) {
  const W = 1000, H = opts.height ?? 300, L = 74, R = 16, T = 14, B = 34;
  const pts = series.flatMap((ser) => ser.points);
  const wrap = el("div", { class: "panel chart-svg" });
  if (pts.length < 2) return wrap.appendChild(empty(opts.emptyText ?? "Not enough data to chart yet.")) && wrap;
  let x0 = opts.xMin ?? Math.min(...pts.map((p) => p[0]));
  let x1 = opts.xMax ?? Math.max(...pts.map((p) => p[0]));
  const ys = pts.map((p) => p[1]).concat((opts.hlines ?? []).map((h) => h.y));
  let y0 = opts.yMin ?? Math.min(...ys), y1 = opts.yMax ?? Math.max(...ys);
  if (y1 === y0) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.08;
  if (opts.yMin == null) y0 -= pad;
  if (opts.yMax == null) y1 += pad;
  if (x1 === x0) x1 = x0 + 1;
  const X = (x) => L + ((x - x0) / (x1 - x0)) * (W - L - R);
  const Y = (y) => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
  const yfmt = opts.yfmt ?? ((v) => v.toFixed(2));
  const xfmt = opts.xfmt ?? ((v) => new Date(v).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "svgchart", role: "img", "aria-label": opts.label ?? "chart" });
  for (const v of niceTicks(y0, y1)) {
    svg.append(s("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: "grid" }), s("text", { x: L - 8, y: Y(v) + 4, class: "tick", "text-anchor": "end" }, yfmt(v)));
  }
  const xt = opts.xTicks ?? Array.from({ length: 5 }, (_, i) => x0 + ((x1 - x0) * i) / 4);
  for (const v of xt) svg.append(s("text", { x: X(v), y: H - 10, class: "tick", "text-anchor": "middle" }, xfmt(v)));
  if (opts.shade) svg.append(s("rect", { x: X(opts.shade[0]), y: T, width: Math.max(0, X(opts.shade[1]) - X(opts.shade[0])), height: H - T - B, class: "shade" }, s("title", {}, opts.shade[2] ?? "")));
  for (const h of opts.hlines ?? []) {
    svg.append(s("line", { x1: L, x2: W - R, y1: Y(h.y), y2: Y(h.y), class: "hline", stroke: h.color ?? "var(--warn)" }, s("title", {}, `${h.label}: ${yfmt(h.y)}`)));
    svg.append(s("text", { x: W - R - 4, y: Y(h.y) - 6, class: "hlabel", "text-anchor": "end", fill: h.color ?? "var(--warn)" }, `${h.label} ${yfmt(h.y)}`));
  }
  for (const ser of series) {
    if (!ser.points.length) continue;
    const d = ser.points.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    svg.append(s("path", { d, class: "series", stroke: ser.color ?? "var(--accent)", "stroke-dasharray": ser.dash ?? null }));
    const step = Math.max(1, Math.floor(ser.points.length / 120));
    ser.points.forEach((p, i) => {
      if (i % step && i !== ser.points.length - 1) return;
      svg.append(s("circle", { cx: X(p[0]), cy: Y(p[1]), r: i === ser.points.length - 1 ? 5 : 7, class: i === ser.points.length - 1 ? "last" : "hit", fill: ser.color ?? "var(--accent)" }, s("title", {}, `${ser.name} · ${xfmt(p[0])}: ${yfmt(p[1])}`)));
    });
  }
  wrap.append(svg);
  if (series.length > 1 || opts.legend) {
    wrap.append(el("div", { class: "legend" }, series.map((ser) => el("span", {}, el("i", { class: "swatch", style: `background:${ser.color ?? "var(--accent)"}` }), ser.name))));
  }
  return wrap;
}

// calibration: points [{p, freq, lo, hi, n}] on a 0..1 square
function calibrationChart(pts, name) {
  const W = 1000, H = 420, L = 74, R = 16, T = 14, B = 40;
  const X = (x) => L + x * (W - L - R);
  const Y = (y) => T + (1 - y) * (H - T - B);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "svgchart", role: "img", "aria-label": "calibration chart" });
  for (const v of [0, 0.25, 0.5, 0.75, 1]) {
    svg.append(s("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), class: "grid" }), s("text", { x: L - 8, y: Y(v) + 4, class: "tick", "text-anchor": "end" }, `${v * 100}%`));
    svg.append(s("text", { x: X(v), y: H - 16, class: "tick", "text-anchor": "middle" }, `${v * 100}%`));
  }
  svg.append(s("line", { x1: X(0), y1: Y(0), x2: X(1), y2: Y(1), class: "hline", stroke: "var(--muted)" }, s("title", {}, "Perfectly honest probabilities")));
  svg.append(s("text", { x: (W + L) / 2, y: H - 1, class: "tick", "text-anchor": "middle" }, "model said P(UP) →"));
  svg.append(s("text", { x: 16, y: (H - B) / 2, class: "tick", "text-anchor": "middle", transform: `rotate(-90 16 ${(H - B) / 2})` }, "actually happened →"));
  for (const q of pts) {
    svg.append(s("line", { x1: X(q.p), x2: X(q.p), y1: Y(q.lo), y2: Y(q.hi), class: "errbar" }));
    svg.append(s("circle", { cx: X(q.p), cy: Y(q.freq), r: 7, class: "last", fill: "var(--accent-2)" }, s("title", {}, `${name}: said ${(q.p * 100).toFixed(0)}% → happened ${(q.freq * 100).toFixed(0)}% (n=${q.n}, 95% range ${(q.lo * 100).toFixed(0)}–${(q.hi * 100).toFixed(0)}%)`)));
  }
  return el("div", { class: "panel chart-svg" }, svg);
}

// "KXBTCD-26OCT1607-T81999.99" -> "BTC above $82,000 · 7:00 AM ET"
function marketName(ticker) {
  const m = /^(KXBTC15M|KXBTCD|KXBTC)-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})?(?:-T?([\d.]+))?/.exec(ticker ?? "");
  if (!m) return ticker;
  const hh = Number(m[5]);
  const mm = m[6] ?? "00";
  const time = `${((hh + 11) % 12) + 1}:${mm} ${hh < 12 ? "AM" : "PM"} ET`;
  if (m[1] === "KXBTC15M") return `15-min BTC up · ${time}`;
  const k = m[7] ? Number(m[7]) + 0.01 : null;
  return k ? `BTC above ${fmt.usd(Math.round(k), 0)} · ${time}` : `BTC · ${time}`;
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
  const warmLeft = (d.min_history_s ?? 0) - (d.history_s ?? 0);
  if (!top && warmLeft > 0) {
    return el("div", { class: "now" },
      el("div", { class: "eyebrow" }, "Right now"),
      el("div", { class: "now-title" }, "Warming up"),
      el("div", { class: "now-sub", title: "After a restart the engine collects 15 minutes of settlement-index ticks before it trusts its volatility estimate. No calls until then." }, "Collecting live index data before making calls. Ready in ", countdown(Date.now() + warmLeft * 1000, ""), "."),
      held.size ? el("div", { class: "now-sub" }, `The paper account is holding ${held.size} position(s) to settlement meanwhile.`) : null,
    );
  }
  const blocked = top && best(top) >= d.config.min_edge && top.reason.startsWith("Waiting");
  if (blocked) {
    const side = (top.edge_up ?? -9) >= (top.edge_down ?? -9) ? "UP" : "DOWN";
    return el("div", { class: "now" },
      el("div", { class: "eyebrow" }, "Right now"),
      el("div", { class: "now-title" }, `Would be BUY ${side} · ${questionFor(top)[0]}`),
      el("div", { class: "now-sub" }, `Expected profit ${fmt.cents(best(top))} clears the ${fmt.cents(d.config.min_edge)} bar, but the engine is holding off. ${top.reason} Do not act until this says BUY.`),
      held.size ? el("div", { class: "now-sub" }, `The paper account is holding ${held.size} position(s); see their HOLD/SELL calls below.`) : null,
    );
  }
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
      ["Spread · depth", `${c.spread == null ? "–" : `${(c.spread * 100).toFixed(1)}¢`} · ${fmt.n(c.depth_up)} UP / ${fmt.n(c.depth_down)} DOWN`, `${TIP.spread} ${TIP.depth}`],
      ["Volatility", c.sigma_per_min_bps ? `${c.sigma_per_min_bps.toFixed(1)} bp/min` : "warming up", TIP.vol],
    ]),
    h ? el("div", { class: "position", title: "The simulated $500k account's position in this market." }, `Paper holds ${fmt.n(h.contracts)} ${h.side} @ ${fmt.price(h.avg_cost)} · unrealized ${h.unrealized == null ? "–" : fmt.usd(h.unrealized, 0)}`) : null,
    el("div", { class: "gates" }, gates.map(([name, ok]) => el("span", { class: `gate ${ok ? "ok" : "no"}`, title: ok ? `Check passed: ${name}` : `Blocking new entries: ${name}` }, `${ok ? "✓" : "✗"} ${name}`))),
  );
  const openMs = closeMs(d, c) - 900_000;
  const pts = (d.index_series ?? []).filter((p) => p[0] >= openMs - 60_000);
  const chart = lineChart([{ name: "BTC index", points: pts }], {
    hlines: c.strike ? [{ y: c.strike, label: "UP needs ≥" }] : [],
    xMin: openMs - 60_000,
    xMax: closeMs(d, c),
    shade: [closeMs(d, c) - 60_000, closeMs(d, c), "Final 60 seconds: their average decides the result"],
    yfmt: (v) => fmt.usd(v, 0),
    height: 560,
    emptyText: "Index data for this window is still arriving.",
    label: "Settlement index this window",
  });
  const right = el("div", {}, el("div", { class: "eyebrow", title: TIP.index }, "Settlement index this window vs the level UP must beat (shaded = final 60 s that decide it)"), chart);
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
    { label: "Call", get: (c) => (c.reason.startsWith("Waiting") ? "waiting" : c.action), tip: `${TIP.action} "waiting" = a data check is failing; hover the 15-minute card's red checks for which.` },
    { label: "Why", get: (c) => c.reason, cls: "wrap", tip: "The engine's reason for this call" },
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
      { label: "Why", get: (e) => e.reason, cls: "wrap" },
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
    root.append(el("h2", { title: "Account value over time, sampled every 10 seconds." }, "Equity"), lineChart([{ name: "Equity", points: eq, color: "var(--accent-2)" }], { yfmt: (v) => fmt.usd(v, 0), hlines: [{ y: p.starting_cash, label: "start", color: "var(--muted)" }], label: "Paper equity" }));
  }
  root.append(el("h2", {}, "Open positions"));
  root.append(p.holdings.length ? table([
    { label: "Market", get: (h) => marketName(h.ticker), tip: "Readable market name (times are US Eastern, Kalshi's time zone)" },
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
    { label: "Market", get: (f) => marketName(f.ticker) },
    { label: "Side", get: (f) => (f.side === "yes" ? "UP" : "DOWN") },
    { label: "Contracts", get: (f) => f.contracts, fmt: fmt.n },
    { label: "Price", get: (f) => f.price, fmt: fmt.price, tip: "Average fill price per contract, before fees (settlement = $1 or $0)" },
    { label: "Fees", get: (f) => f.fees, fmt: (v) => fmt.usd(v, 0) },
    { label: "P&L", get: (f) => f.pnl, fmt: (v) => (v == null ? "–" : fmt.usd(v, 0)), tone: true },
    { label: "Why", get: (f) => f.reason, cls: "wrap" },
  ], fills) : empty("No trades yet. The engine only buys when a market is mispriced by at least the minimum edge; on most minutes it correctly waits."));
}

const STRATEGY_NAMES = {
  market: "Kalshi's own price (benchmark)",
  model_t4: "Our fair-value model",
  blend: "Model + Kalshi blend",
  logreg_all: "Logistic regression, all signals",
  gbm: "Gradient-boosted trees, all signals",
};

async function research(root) {
  const r = (await load("research"))?.data;
  root.append(el("div", { class: "banner" }, "Bottom line from 30 days of past markets: no strategy tested so far makes money after fees. Kalshi's own prices are usually as accurate as our model. Treat every call as unproven until the paper account shows profit over hundreds of trades."));
  const series = r?.series ?? {};
  if (!Object.keys(series).length) return root.append(empty("No research report published yet."));
  const rows = [];
  for (const [s, v] of Object.entries(series)) for (const st of v.strategies) rows.push({ s, name: st.name, all: st.all_oos, hold: st.holdout });
  root.append(table([
    { label: "Market", get: (x) => (x.s === "KXBTC15M" ? "15-minute" : "Hourly") },
    { label: "Strategy", get: (x) => STRATEGY_NAMES[x.name] ?? x.name, tip: "Each row is one way of estimating the chance of UP, tested only on days it never saw." },
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
  const holder = el("div");
  root.append(pick, holder);
  const draw = () => {
    const [hm, m] = pick.value.split("|");
    const b = hs.find((h) => String(h.horizon_min) === hm).models[m].buckets.filter((x) => x.n && x.mean_p != null && x.freq != null);
    holder.replaceChildren(b.length ? calibrationChart(b.map((x) => ({ p: x.mean_p, freq: x.freq, lo: x.ci_lo, hi: x.ci_hi, n: x.n })), m) : empty("No buckets with data for this choice."));
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
    { label: "Status", get: (x) => (x[1].active ? "live" : x[1].staleness_ms == null ? "not connected" : "idle (no price change)"), tip: "Idle means the exchange simply hasn't changed its best price recently; idle exchanges are left out of the average until they update." },
    { label: "Quote age", get: (x) => x[1].staleness_ms, fmt: fmt.age, tip: "How old that exchange's best bid/offer is" },
    { label: "Latency", get: (x) => x[1].latency_ms, fmt: (v) => (v == null ? "–" : `${v} ms`), tip: "Exchange timestamp to our receive time" },
  ], ex) : empty("Exchange feeds not connected yet."));
}

const RENDER = { live, paper, research, calibration, system };

let render = async function () {
  const root = document.getElementById("content");
  document.getElementById("page-title").textContent = PAGES[page];
  const frag = el("div");
  try {
    await RENDER[page](frag);
  } catch (e) {
    frag.replaceChildren(empty(`This page hit an error: ${e.message}`));
  }
  root.replaceChildren(...frag.childNodes);
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
  location.hash = a.dataset.page;
});
window.addEventListener("hashchange", () => {
  if (PAGES[location.hash.slice(1)]) {
    page = location.hash.slice(1);
    render();
  }
});
document.getElementById("refresh").addEventListener("click", render);
const _render = render;
render = async () => {
  for (const x of document.querySelectorAll("#nav a")) x.classList.toggle("active", x.dataset.page === page);
  await _render();
};
render();
// live countdowns tick every second between data refreshes
setInterval(() => {
  for (const n of document.querySelectorAll("[data-close]")) n.textContent = fmt.left((Number(n.dataset.close) - Date.now()) / 1000);
}, 1000);
