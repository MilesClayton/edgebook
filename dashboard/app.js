// edgebook dashboard. Page structure (sidebar router, metric-card rows,
// refresh control, Plotly panels) adapted from ryanfrigo/kalshi-ai-trading-bot
// scripts/trading_dashboard.py, MIT License; see THIRD_PARTY_LICENSES.md.
"use strict";

const PAGES = {
  overview: "Overview",
  live: "Live Markets",
  paper: "Paper Session",
  lab: "Strategy Lab",
  calibration: "Calibration",
  trades: "Trades",
  health: "System Health",
};
const cache = {};
let page = "overview";
let timer = null;

// ---------- data ----------
async function load(kind, fresh = false) {
  if (!fresh && cache[kind] && Date.now() - cache[kind].at < 20_000) return cache[kind].v;
  try {
    const r = await fetch(`/api/data?kind=${kind}`, { cache: "no-store" });
    const v = r.ok ? await r.json() : null;
    cache[kind] = { v, at: Date.now() };
    return v;
  } catch {
    return null;
  }
}

// ---------- tiny DOM helpers (textContent only; no HTML injection) ----------
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else n.setAttribute(k, v);
  }
  for (const k of kids.flat()) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return n;
}
const fmt = {
  usd: (x, d = 2) => (x == null ? "–" : `$${Number(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`),
  c: (x) => (x == null ? "–" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)}¢`),
  p: (x) => (x == null ? "–" : Number(x).toFixed(3)),
  n: (x) => (x == null ? "–" : Number(x).toLocaleString()),
  ago: (iso) => {
    if (!iso) return "never";
    const s = (Date.now() - Date.parse(iso)) / 1000;
    return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : `${(s / 3600).toFixed(1)}h ago`;
  },
};
function card(k, v, s = "", tone = "") {
  return el("div", { class: `card ${tone}` }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), el("div", { class: "s" }, s));
}
function table(cols, rows) {
  const head = el("tr", {}, cols.map((c) => el("th", {}, c.label)));
  const body = rows.map((r, i) =>
    el("tr", {}, cols.map((c) => {
      const v = c.get(r, i);
      const td = el("td", {}, c.fmt ? c.fmt(v) : v ?? "–");
      if (c.tone && v != null) td.className = v > 0 ? "pos" : v < 0 ? "neg" : "";
      return td;
    })),
  );
  return el("div", { class: "panel" }, el("table", {}, el("thead", {}, head), el("tbody", {}, body)));
}
function empty(msg) {
  return el("div", { class: "panel empty" }, msg);
}
function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function plot(node, traces, layout = {}) {
  if (!window.Plotly) return;
  const base = {
    margin: { l: 50, r: 16, t: 30, b: 40 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: css("--text"), size: 12 },
    xaxis: { gridcolor: css("--border") },
    yaxis: { gridcolor: css("--border") },
    legend: { orientation: "h", y: -0.2 },
  };
  Plotly.newPlot(node, traces, { ...base, ...layout }, { displayModeBar: false, responsive: true });
}

function spotChart(node, series) {
  const data = series.map(([time, value]) => {
    const raw = typeof time === "number" ? time : Date.parse(time);
    return { time: Math.floor(raw < 1e12 ? raw : raw / 1000), value: Number(value) };
  })
    .filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value));
  if (data.length < 2) {
    node.replaceWith(empty("BTC reference data is not available in this snapshot."));
    return;
  }
  if (!window.Plotly) return spotSvg(node, data);
  const rising = data.at(-1).value >= data[0].value;
  const color = rising ? css("--accent") : css("--bad");
  plot(node, [{
    x: data.map((point) => new Date(point.time * 1000)),
    y: data.map((point) => point.value),
    type: "scatter", mode: "lines", name: "BTC-USD reference", fill: "tozeroy",
    line: { color, width: 2.5, shape: "spline", smoothing: 0.45 },
    fillcolor: rising ? "rgba(114,241,197,.12)" : "rgba(255,139,158,.10)",
    hovertemplate: "%{x|%b %-d, %H:%M}<br><b>$%{y:,.2f}</b><extra></extra>",
  }], {
    margin: { l: 70, r: 20, t: 18, b: 42 }, hovermode: "x unified", showlegend: false,
    xaxis: { gridcolor: css("--border"), rangeslider: { visible: false }, fixedrange: false },
    yaxis: { gridcolor: css("--border"), tickprefix: "$", tickformat: ",.0f", side: "right", fixedrange: false },
  });
}

function spotSvg(node, data) {
  const width = 1100, height = 390, pad = { top: 26, right: 84, bottom: 42, left: 20 };
  const values = data.map((point) => point.value), low = Math.min(...values), high = Math.max(...values);
  const range = high - low || 1, innerW = width - pad.left - pad.right, innerH = height - pad.top - pad.bottom;
  const x = (i) => pad.left + i / (data.length - 1) * innerW;
  const y = (value) => pad.top + (high - value) / range * innerH;
  const path = values.map((value, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(value).toFixed(1)}`).join(" ");
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "spot-svg"); svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const defs = document.createElementNS(ns, "defs"), gradient = document.createElementNS(ns, "linearGradient");
  gradient.setAttribute("id", "spot-fill"); gradient.setAttribute("x1", "0"); gradient.setAttribute("x2", "0"); gradient.setAttribute("y1", "0"); gradient.setAttribute("y2", "1");
  [["0%", "#72f1c5", ".32"], ["100%", "#72f1c5", "0"]].forEach(([offset, color, opacity]) => { const stop = document.createElementNS(ns, "stop"); stop.setAttribute("offset", offset); stop.setAttribute("stop-color", color); stop.setAttribute("stop-opacity", opacity); gradient.append(stop); });
  defs.append(gradient); svg.append(defs);
  for (const r of [0, .25, .5, .75, 1]) {
    const yy = pad.top + r * innerH, value = high - r * range;
    const grid = document.createElementNS(ns, "line"); grid.setAttribute("x1", String(pad.left)); grid.setAttribute("x2", String(width - pad.right)); grid.setAttribute("y1", String(yy)); grid.setAttribute("y2", String(yy)); grid.setAttribute("class", "spot-grid"); svg.append(grid);
    const label = document.createElementNS(ns, "text"); label.setAttribute("x", String(width - pad.right + 10)); label.setAttribute("y", String(yy + 4)); label.setAttribute("class", "spot-label"); label.textContent = fmt.usd(value, 0); svg.append(label);
  }
  const area = document.createElementNS(ns, "path"); area.setAttribute("d", `${path} L${x(data.length - 1)} ${pad.top + innerH} L${x(0)} ${pad.top + innerH} Z`); area.setAttribute("fill", "url(#spot-fill)"); svg.append(area);
  const line = document.createElementNS(ns, "path"); line.setAttribute("d", path); line.setAttribute("class", "spot-line"); svg.append(line);
  const last = document.createElementNS(ns, "circle"); last.setAttribute("cx", String(x(data.length - 1))); last.setAttribute("cy", String(y(values.at(-1)))); last.setAttribute("r", "5"); last.setAttribute("class", "spot-dot"); svg.append(last);
  [[0, "start"], [Math.floor((data.length - 1) / 2), "middle"], [data.length - 1, "end"]].forEach(([index, anchor]) => { const label = document.createElementNS(ns, "text"); label.setAttribute("x", String(x(index))); label.setAttribute("y", String(height - 12)); label.setAttribute("text-anchor", anchor); label.setAttribute("class", "spot-time"); label.textContent = new Date(data[index].time * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); svg.append(label); });
  node.replaceChildren(svg);
}

function equityChart(node, history) {
  const points = history.map((event) => ({
    time: Date.parse(event.at), value: Number(event.state?.equity), event: event.event,
  })).filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value));
  if (!points.length) {
    node.replaceWith(empty("Equity history begins with the next paper decision."));
    return;
  }
  if (points.length === 1) points.unshift({ ...points[0], time: points[0].time - 1_000 });
  spotSvg(node, points.map((point) => ({ time: Math.floor(point.time / 1000), value: point.value })));
}

function executionHistory(history, state) {
  const config = state?.config ?? {};
  let cash = Number(config.paper_starting_cash ?? 0);
  let reserved = 0;
  let realized = 0;
  let fees = 0;
  return history.map((event) => {
    const detail = event.detail ?? {};
    if (event.event === "session_started") {
      cash = Number(config.paper_starting_cash ?? cash);
      reserved = 0;
      realized = 0;
      fees = 0;
    } else if (event.event === "paper_filled") {
      cash -= Number(detail.debit ?? 0);
      reserved += Number(detail.debit ?? 0);
      fees += Number(detail.fee ?? 0);
    } else if (event.event === "paper_settled") {
      cash += Number(detail.payout ?? 0);
      reserved -= Number(detail.debit ?? 0);
      realized += Number(detail.pnl ?? 0);
    }
    // New records have an authoritative compact snapshot.  Older records are
    // reconstructed from their fill/settlement detail so the first session is
    // visible too.
    const snapshot = event.state ?? {};
    const point = {
      cash: Number(snapshot.cash ?? cash),
      reserved: Number(snapshot.reserved ?? reserved),
      equity: Number(snapshot.equity ?? cash + reserved),
      realized_pnl: Number(snapshot.realized_pnl ?? realized),
      fees: Number(snapshot.fees ?? fees),
      open_positions: Number(snapshot.open_positions ?? (reserved > 0 ? 1 : 0)),
      max_drawdown: Number(snapshot.max_drawdown ?? 0),
    };
    cash = point.cash;
    reserved = point.reserved;
    realized = point.realized_pnl;
    fees = point.fees;
    return { ...event, state: point };
  });
}

function liveGate(research) {
  const candidates = [];
  for (const [series, report] of Object.entries(research?.data?.series ?? {})) {
    for (const strategy of report.strategies ?? []) {
      const result = strategy.holdout?.taker?.["0.02"];
      if (!result) continue;
      candidates.push({
        series,
        name: strategy.name,
        n: result.n ?? 0,
        lo: result.ci_lo,
        brier: strategy.holdout?.brier,
        marketBrier: strategy.holdout?.market_brier,
      });
    }
  }
  const approved = candidates.filter((x) =>
    x.n >= 500 && x.lo > 0 && x.brier <= x.marketBrier,
  );
  return { candidates, approved };
}

function decisionRows(snapshot) {
  return Object.entries(snapshot?.data?.signals ?? {}).flatMap(([series, list]) =>
    list.map((signal) => {
      const side = signal.edge_yes >= signal.edge_no ? "YES" : "NO";
      const edge = Math.max(signal.edge_yes, signal.edge_no);
      const entry = side === "YES" ? signal.ask : 1 - signal.bid;
      const sideFair = side === "YES" ? signal.fair : 1 - signal.fair;
      return { ...signal, series, side, edge, entry, sideFair, minutes: signal.seconds_left / 60 };
    }),
  ).sort((a, b) => b.edge - a.edge);
}

function ledger(trades) {
  const list = trades?.data ?? [];
  const reserved = list.reduce((sum, row) => sum + Number(row.reserved_dollars ?? 0), 0);
  const cap = list.at(-1)?.session_cap_dollars ?? 5;
  return { list, cap, reserved, remaining: Math.max(0, cap - reserved) };
}

function gateReason(gate) {
  if (gate.approved.length) return "paper evidence is ready for review";
  const best = gate.candidates.sort((a, b) => (b.lo ?? -Infinity) - (a.lo ?? -Infinity))[0];
  if (!best) return "no holdout report has been published";
  if ((best.n ?? 0) < 500) return `only ${fmt.n(best.n)} holdout trades`;
  if (!(best.lo > 0)) return "the best 95% confidence bound is not positive";
  return "model calibration is weaker than the market";
}

// ---------- pages ----------
async function overview(root) {
  const [snap, research, trades] = await Promise.all([load("snapshot"), load("research"), load("trades")]);
  const s = snap?.data;
  const rec = s?.recorder ?? {};
  const gate = liveGate(research);
  const candidates = decisionRows(snap);
  const lead = candidates[0];
  const book = ledger(trades);
  const flow = s?.public_flow ?? {};
  const flowWindows = flow.windows ?? [];
  const decision = gate.approved.length && lead?.edge > 0.02 ? "REVIEW" : "OBSERVE";
  const bestEvidence = gate.candidates.sort((a, b) => (b.lo ?? -Infinity) - (a.lo ?? -Infinity))[0];
  root.append(
    el("section", { class: "hero" },
      el("div", {},
        el("div", { class: "eyebrow" }, "LIVE DECISION DESK"),
        el("div", { class: "hero-value" }, fmt.usd(s?.spot, 2)),
        el("div", { class: "muted" }, `BTC-USD composite · snapshot ${fmt.ago(snap?.received_at)}`),
      ),
      el("div", { class: `decision ${decision === "REVIEW" ? "good" : "warn"}` },
        el("div", { class: "eyebrow" }, "EXECUTION STATE"),
        el("div", { class: "decision-value" }, decision),
        el("div", { class: "muted" }, gateReason(gate)),
      ),
    ),
    el("div", { class: "cards" },
      card("Best quoted edge", lead ? fmt.c(lead.edge) : "–", lead ? `${lead.series} · buy ${lead.side}` : "no live candidate", lead?.edge > 0 ? "good" : ""),
      card("Entry debit", lead ? fmt.usd(lead.entry) : "–", lead ? `${lead.minutes.toFixed(1)} min to close` : "waiting for market"),
      card("Best holdout CI low", bestEvidence ? fmt.c(bestEvidence.lo) : "–", bestEvidence ? `${bestEvidence.series} · n=${fmt.n(bestEvidence.n)}` : "no holdout evidence", bestEvidence?.lo > 0 ? "good" : "bad"),
      card("Session budget", fmt.usd(book.remaining), `${fmt.usd(book.reserved)} reserved of ${fmt.usd(book.cap)}`, book.remaining > 0 ? "good" : "bad"),
      card("Feed quality", rec.msgs_per_s != null ? `${Math.round(rec.msgs_per_s)}/s` : "–", `${fmt.n(rec.gaps)} gaps · ${fmt.n(rec.reconnects)} reconnects`, rec.gaps ? "bad" : "good"),
    ),
  );
  const chart = el("div", { class: "panel chart chart-large" });
  root.append(el("h2", {}, "BTC reference price"), chart);
  const series = s?.spot_series ?? [];
  if (series.length) {
    spotChart(chart, series);
  } else chart.replaceWith(empty("No snapshot yet: the Fly recorder publishes one every minute."));

  root.append(el("h2", {}, "Public trade flow"));
  root.append(el("p", { class: "muted" }, flow.label ?? "Anonymous public trade flow from Kalshi’s exchange feed."));
  root.append(flowWindows.length ? el("div", { class: "cards" }, flowWindows.map((window) => {
    const signed = Number(window.net_yes_contracts ?? 0);
    const direction = signed > 0 ? "net YES takers" : signed < 0 ? "net NO takers" : "balanced";
    return card(`${window.seconds / 60} min flow`, `${signed >= 0 ? "+" : ""}${fmt.n(signed)} ct`, `${fmt.n(window.trades)} trades · ${direction}`, signed > 0 ? "good" : signed < 0 ? "bad" : "");
  })) : empty("Waiting for public trades from the live Kalshi feed."));
  if (flow.recent?.length) {
    root.append(table([
      { label: "Observed", get: (r) => new Date(r.observed_ts_ms).toLocaleTimeString() },
      { label: "Contract", get: (r) => r.ticker },
      { label: "Taker", get: (r) => String(r.taker_side).toUpperCase() },
      { label: "Contracts", get: (r) => r.count, fmt: fmt.n },
      { label: "YES price", get: (r) => r.yes_price, fmt: fmt.usd },
    ], flow.recent.slice().reverse()));
  }

  root.append(el("h2", {}, "Decision queue"));
  root.append(candidates.length ? table([
    { label: "Contract", get: (r) => r.ticker },
    { label: "Side", get: (r) => r.side },
    { label: "Entry", get: (r) => r.entry, fmt: fmt.usd },
    { label: "Model P(side)", get: (r) => r.sideFair, fmt: fmt.p },
    { label: "Net edge", get: (r) => r.edge, fmt: fmt.c, tone: true },
    { label: "Time left", get: (r) => `${r.minutes.toFixed(1)}m` },
  ], candidates.slice(0, 6)) : empty("No eligible markets in the latest snapshot."));

  root.append(el("h2", {}, "Research verdict · out of sample after fees"));
  const rows = [];
  for (const [series, r] of Object.entries(research?.data?.series ?? {})) {
    for (const st of r.strategies) {
      const t = st.holdout.taker["0.02"];
      rows.push({ series, name: st.name, n: t?.n, mean: t?.mean, lo: t?.ci_lo, hi: t?.ci_hi });
    }
  }
  root.append(rows.length
    ? table([
        { label: "Series", get: (r) => r.series },
        { label: "Strategy", get: (r) => r.name },
        { label: "Holdout trades", get: (r) => r.n, fmt: fmt.n },
        { label: "Mean P&L / contract", get: (r) => r.mean, fmt: fmt.c, tone: true },
        { label: "95% CI", get: (r) => (r.n > 1 ? `${fmt.c(r.lo)} … ${fmt.c(r.hi)}` : "–") },
        { label: "Verdict", get: (r) => (r.lo > 0 ? "edge" : r.hi < 0 ? "loses" : "unproven") },
      ], rows)
    : empty("No research published yet: run `edgebook research --publish`."));
}

async function live(root) {
  const [snap, research] = await Promise.all([load("snapshot", true), load("research")]);
  const sigs = snap?.data?.signals ?? {};
  const gate = liveGate(research);
  const rows = decisionRows(snap);
  root.append(el("div", { class: "status-strip" },
    el("span", { class: "pill good" }, `snapshot ${fmt.ago(snap?.received_at)}`),
    el("span", { class: `pill ${gate.approved.length ? "warn" : "bad"}` }, gate.approved.length ? "paper review" : "execution blocked"),
    el("span", { class: "muted" }, "Each edge includes the Kalshi taker fee. A displayed signal is a quote comparison, not an order."),
  ));
  root.append(rows.length ? table([
    { label: "Priority", get: (_, i) => `#${i + 1}` },
    { label: "Market", get: (r) => r.ticker },
    { label: "Action", get: (r) => `Buy ${r.side}` },
    { label: "Entry", get: (r) => r.entry, fmt: fmt.usd },
    { label: "Model P(side)", get: (r) => r.sideFair, fmt: fmt.p },
    { label: "Net edge", get: (r) => r.edge, fmt: fmt.c, tone: true },
    { label: "Close", get: (r) => `${r.minutes.toFixed(1)}m` },
  ], rows.slice(0, 8)) : empty("No live candidate is currently priceable."));
  root.append(el("h2", {}, "Market depth by series"));
  for (const [series, list] of Object.entries(sigs)) {
    root.append(el("h2", {}, series === "KXBTC15M" ? "15-minute up/down (KXBTC15M)" : `${series}: next close, nearest strikes`));
    root.append(list.length
      ? table([
          { label: "Market", get: (r) => r.ticker },
          { label: "Strike", get: (r) => r.floor ?? r.cap, fmt: (v) => fmt.usd(v) },
          { label: "Min left", get: (r) => (r.seconds_left / 60).toFixed(1) },
          { label: "Fair", get: (r) => r.fair, fmt: fmt.p },
          { label: "Bid", get: (r) => r.bid, fmt: fmt.p },
          { label: "Ask", get: (r) => r.ask, fmt: fmt.p },
          { label: "Edge YES", get: (r) => r.edge_yes, fmt: fmt.c, tone: true },
          { label: "Edge NO", get: (r) => r.edge_no, fmt: fmt.c, tone: true },
        ], list)
      : empty("No open market right now."));
  }
  if (!Object.keys(sigs).length) root.append(empty("No snapshot yet."));
}

async function paper(root) {
  const execution = await load("execution", true);
  // A state-only payload keeps this page compatible with deployments before
  // the history feed; new deployments provide {state, history}.
  const state = execution?.data?.state ?? execution?.data;
  const history = executionHistory(execution?.data?.history ?? [], state);
  const current = state?.config ?? {};
  const budget = el("input", { type: "number", min: "1", step: "0.01", value: current.paper_starting_cash ?? "5" });
  const perOrder = el("input", { type: "number", min: "0.01", step: "0.01", value: current.max_order_debit ?? "1" });
  const maxOpen = el("input", { type: "number", min: "0.01", step: "0.01", value: current.max_open_debit ?? "5" });
  const minEdge = el("input", { type: "number", min: "0", step: "0.001", value: current.min_edge ?? "0.02" });
  const notice = el("div", { class: "muted control-notice" }, "Paper controls are protected by the dashboard passcode.");
  const send = async (action) => {
    notice.textContent = `${action === "start" ? "Starting" : "Stopping"} paper session…`;
    try {
      const response = await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action,
          config: action === "start" ? {
            mode: "paper",
            paper_starting_cash: budget.value,
            max_order_debit: perOrder.value,
            max_open_debit: maxOpen.value,
            min_edge: minEdge.value,
            max_open_positions: 1,
          } : undefined,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "request failed");
      notice.textContent = "Command accepted. The recorder applies it on its next minute cycle.";
      delete cache.execution;
      setTimeout(render, 1500);
    } catch (error) {
      notice.textContent = `Control failed: ${error.message}`;
    }
  };
  const start = el("button", { type: "button", class: "primary" }, "Start paper trading");
  start.addEventListener("click", () => send("start"));
  const stop = el("button", { type: "button", class: "danger" }, "Stop");
  stop.addEventListener("click", () => send("stop"));
  root.append(el("section", { class: "panel controls" },
    el("div", { class: "control-heading" }, el("div", { class: "eyebrow" }, "PAPER EXECUTION"), el("strong", {}, "Real quotes · simulated fills")),
    el("div", { class: "control-grid" },
      el("label", {}, "Starting balance ($)", budget),
      el("label", {}, "Per order cap ($)", perOrder),
      el("label", {}, "Open debit cap ($)", maxOpen),
      el("label", {}, "Minimum net edge ($)", minEdge),
      el("div", { class: "control-actions" }, start, stop),
    ),
    notice,
  ));
  if (!state) {
    return root.append(empty("No paper session has been published yet. Start one on the recorder, then this page will show its real quote fills and settlements."));
  }
  const config = state.config ?? {};
  const settled = state.settled ?? [];
  const open = state.positions ?? [];
  const reserved = open.reduce((total, position) => total + Number(position.debit ?? 0), 0);
  const equity = Number(state.paper_cash ?? 0) + reserved;
  root.append(el("div", { class: "status-strip" },
    el("span", { class: `pill ${state.status === "running" ? "good" : "warn"}` }, state.status),
    el("span", { class: "pill warn" }, "paper money"),
    el("span", { class: "muted" }, `updated ${fmt.ago(execution.received_at)} · quote fills include Kalshi taker fees`),
  ));
  root.append(el("div", { class: "cards" },
    card("Paper equity", fmt.usd(equity), `starting ${fmt.usd(config.paper_starting_cash)}`),
    card("Available cash", fmt.usd(state.paper_cash), `${fmt.usd(reserved)} open debit`),
    card("Realized P&L", fmt.usd(state.paper_realized_pnl), `${fmt.usd(state.paper_fees)} fees paid`, Number(state.paper_realized_pnl) >= 0 ? "good" : "bad"),
    card("Max drawdown", fmt.usd(state.paper_max_drawdown), `limit ${fmt.usd(config.max_open_debit)} open debit`, Number(state.paper_max_drawdown) > 0 ? "bad" : "good"),
    card("Trade threshold", fmt.c(config.min_edge), `max ${fmt.usd(config.max_order_debit)} per order`),
  ));
  root.append(el("h2", {}, "Paper equity · actual session events"));
  const equityPanel = el("div", { class: "panel chart chart-equity" });
  root.append(equityPanel);
  equityChart(equityPanel, history);
  root.append(el("p", { class: "muted" }, "Equity equals available paper cash plus debit reserved for open positions. It is not marked to market until Kalshi settles the contract."));
  root.append(el("h2", {}, "Decision timeline"));
  const meaningful = history.filter((item) => ["session_started", "paper_filled", "paper_settled", "decision_skipped", "session_stopped"].includes(item.event));
  root.append(meaningful.length ? table([
    { label: "Time", get: (r) => new Date(r.at).toLocaleString() },
    { label: "Event", get: (r) => r.event.replaceAll("_", " ") },
    { label: "Equity", get: (r) => r.state?.equity, fmt: fmt.usd },
    { label: "Cash", get: (r) => r.state?.cash, fmt: fmt.usd },
    { label: "Open", get: (r) => r.state?.open_positions, fmt: fmt.n },
    { label: "Reason", get: (r) => r.detail?.reason ?? r.detail?.ticker ?? "–" },
  ], meaningful.slice().reverse().slice(0, 30)) : empty("No decision events have been published yet."));
  root.append(el("h2", {}, "Open paper positions"));
  root.append(open.length ? table([
    { label: "Market", get: (r) => r.ticker },
    { label: "Side", get: (r) => r.side },
    { label: "Entry", get: (r) => r.entry_cost, fmt: fmt.usd },
    { label: "Fee", get: (r) => r.fee, fmt: fmt.usd },
    { label: "Debit", get: (r) => r.debit, fmt: fmt.usd },
    { label: "Model edge", get: (r) => r.edge, fmt: fmt.c, tone: true },
    { label: "Opened", get: (r) => new Date(r.opened_at).toLocaleString() },
  ], open) : empty("No open paper positions."));
  root.append(el("h2", {}, "Settled paper trades"));
  root.append(settled.length ? table([
    { label: "Market", get: (r) => r.ticker },
    { label: "Side", get: (r) => r.side },
    { label: "Result", get: (r) => r.result.toUpperCase() },
    { label: "Debit", get: (r) => r.debit, fmt: fmt.usd },
    { label: "Payout", get: (r) => r.payout, fmt: fmt.usd },
    { label: "P&L", get: (r) => r.pnl, fmt: fmt.usd, tone: true },
    { label: "Settled", get: (r) => new Date(r.settled_at).toLocaleString() },
  ], settled.slice().reverse()) : empty("No paper positions have settled."));
}

async function lab(root) {
  const research = await load("research");
  const series = research?.data?.series ?? {};
  if (!Object.keys(series).length) return root.append(empty("No research published yet: run `edgebook research --days 30 --publish`."));
  root.append(el("p", { class: "muted" }, `Walk-forward: each day is predicted by a model fit only on earlier days. The last ${research.data.holdout_days} days are a separate holdout. Generated ${fmt.ago(research.data.generated)} on ${research.data.days} days of history.`));
  const gate = liveGate(research);
  const bestGate = gate.candidates.sort((a, b) => (b.lo ?? -Infinity) - (a.lo ?? -Infinity))[0];
  root.append(el("div", { class: "cards" },
    card("Execution status", gate.approved.length ? "PAPER REVIEW" : "BLOCKED", "requires ≥500 holdout trades, positive 95% CI, and Brier no worse than market", gate.approved.length ? "warn" : "bad"),
    card("Best holdout CI low", bestGate ? fmt.c(bestGate.lo) : "–", bestGate ? `${bestGate.series} / ${bestGate.name}, n=${fmt.n(bestGate.n)}` : "no evaluated candidate", bestGate?.lo > 0 ? "good" : "bad"),
  ));
  for (const [name, r] of Object.entries(series)) {
    root.append(el("h2", {}, `${name}: ${fmt.n(r.rows)} decision points`));
    const rows = r.strategies.flatMap((st) => ["all_oos", "holdout"].map((period) => ({ st, period, pr: st[period] })));
    root.append(table([
      { label: "Strategy", get: (x) => x.st.name },
      { label: "Period", get: (x) => (x.period === "all_oos" ? "all OOS" : "holdout") },
      { label: "Brier", get: (x) => x.pr.brier, fmt: (v) => (v == null ? "–" : v.toFixed(4)) },
      { label: "Market Brier", get: (x) => x.pr.market_brier, fmt: (v) => (v == null ? "–" : v.toFixed(4)) },
      ...["0.0", "0.02", "0.05"].map((th) => ({ label: `Taker >${th}`, get: (x) => x.pr.taker[th]?.mean, fmt: (v) => fmt.c(v), tone: true })),
      { label: "Trades >0.02", get: (x) => x.pr.taker["0.02"]?.n, fmt: fmt.n },
    ], rows));
    const eq = el("div", { class: "panel chart" });
    root.append(eq);
    plot(eq, r.strategies.filter((st) => st.equity?.length).map((st) => ({
      x: st.equity.map((p) => new Date(p[0] * 1000)), y: st.equity.map((p) => p[1]), type: "scatter", mode: "lines", name: st.name,
    })), { title: { text: "Out-of-sample equity, $ per 1-contract taker trade (edge > 2¢)", font: { size: 13 } }, yaxis: { gridcolor: css("--border"), tickprefix: "$" } });
    if (r.price_bias?.length) {
      root.append(el("h3", {}, "Price-level bias: always buy YES or NO by price bucket"));
      root.append(table([
        { label: "Mid bucket", get: (b) => `${b.lo.toFixed(2)}–${b.hi.toFixed(2)}` },
        { label: "n", get: (b) => b.n, fmt: fmt.n },
        { label: "Buy YES", get: (b) => b.yes.mean, fmt: fmt.c, tone: true },
        { label: "Buy NO", get: (b) => b.no.mean, fmt: fmt.c, tone: true },
      ], r.price_bias));
    }
  }
}

async function calibration(root) {
  const cal = await load("calibration");
  const hs = cal?.data?.horizons ?? [];
  if (!hs.length) return root.append(empty("No calibration published yet: run `edgebook calibrate --publish`."));
  const d = cal.data;
  root.append(el("div", { class: "cards" },
    card("Rows scored", fmt.n(d.n_scored), `of ${fmt.n(d.n_rows_in)}`),
    card("Coinbase vs settlement", `${d.basis_bps.median?.toFixed(1)} bps`, `p90 ${d.basis_bps.p90?.toFixed(1)} bps`),
    card("Published", fmt.ago(cal.received_at)),
  ));
  root.append(table([
    { label: "Minutes before close", get: (h) => h.horizon_min },
    { label: "n", get: (h) => h.n, fmt: fmt.n },
    { label: "Market Brier", get: (h) => h.market_brier.toFixed(4) },
    ...Object.keys(hs[0].models).map((m) => ({ label: `${m} Brier`, get: (h) => h.models[m].brier.toFixed(4) })),
  ], hs));
  const pick = el("select", {}, hs.flatMap((h) => Object.keys(h.models).map((m) => el("option", { value: `${h.horizon_min}|${m}` }, `${h.horizon_min} min: ${m}`))));
  const chart = el("div", { class: "panel chart" });
  root.append(el("h2", {}, "Reliability: predicted vs realized"), pick, chart);
  const draw = () => {
    const [hm, m] = pick.value.split("|");
    const b = hs.find((h) => String(h.horizon_min) === hm).models[m].buckets.filter((x) => x.n);
    plot(chart, [
      { x: [0, 1], y: [0, 1], mode: "lines", line: { dash: "dot", color: css("--muted") }, name: "perfect" },
      { x: b.map((x) => x.mean_p), y: b.map((x) => x.freq), mode: "markers+lines", name: m, error_y: { type: "data", symmetric: false, array: b.map((x) => x.ci_hi - x.freq), arrayminus: b.map((x) => x.freq - x.ci_lo) }, text: b.map((x) => `n=${x.n}`) },
    ], { xaxis: { title: "predicted P(YES)", range: [0, 1], gridcolor: css("--border") }, yaxis: { title: "realized frequency", range: [0, 1], gridcolor: css("--border") } });
  };
  pick.addEventListener("change", draw);
  draw();
}

async function trades(root) {
  const t = await load("trades", true);
  const list = t?.data ?? [];
  const book = ledger(t);
  root.append(el("div", { class: "cards" },
    card("Session cap", fmt.usd(book.cap), "all submitted order debits"),
    card("Reserved", fmt.usd(book.reserved), `${fmt.usd(book.remaining)} remaining`, book.remaining > 0 ? "good" : "bad"),
    card("Orders", fmt.n(list.length), "IOC fills and cancellations are logged"),
  ));
  if (!list.length) return root.append(empty("No orders yet. The budget is ready, but execution remains blocked until the statistical gate passes."));
  root.append(table([
    { label: "Time", get: (r) => new Date(r.ts).toLocaleString() },
    { label: "Env", get: (r) => r.env },
    { label: "Market", get: (r) => r.order.ticker },
    { label: "Side", get: (r) => (r.order.side === "bid" ? "YES" : "NO") },
    { label: "Price", get: (r) => (r.order.side === "bid" ? r.order.price : (1 - r.order.price).toFixed(4)) },
    { label: "Reserved", get: (r) => r.reserved_dollars, fmt: fmt.usd },
    { label: "Filled", get: (r) => r.response.fill_count },
    { label: "Fee", get: (r) => r.response.average_fee_paid },
    { label: "Model edge", get: (r) => Math.max(r.signal.edge_yes, r.signal.edge_no), fmt: fmt.c, tone: true },
    { label: "Result", get: (r) => r.result ?? "pending" },
  ], list.slice().reverse()));
}

async function health(root) {
  const snap = await load("snapshot", true);
  const rec = snap?.data?.recorder;
  if (!rec) return root.append(empty("No recorder snapshot yet."));
  const disk = rec.disk_total_gb ? rec.disk_used_gb / rec.disk_total_gb : null;
  root.append(el("div", { class: "cards" },
    card("Snapshot age", fmt.ago(snap.received_at), "published every 60s", Date.now() - Date.parse(snap.received_at) > 180_000 ? "bad" : "good"),
    card("Messages / s", rec.msgs_per_s != null ? Math.round(rec.msgs_per_s) : "–", `${fmt.n(rec.messages)} since start`),
    card("Sequence gaps", fmt.n(rec.gaps), `${fmt.n(rec.reconnects)} reconnects`, rec.gaps ? "bad" : "good"),
    card("Disk", disk == null ? "–" : `${(disk * 100).toFixed(0)}%`, `${rec.disk_used_gb?.toFixed(1)} / ${rec.disk_total_gb?.toFixed(0)} GB`, disk > 0.8 ? "bad" : "good"),
    card("Uptime", rec.uptime_s ? `${(rec.uptime_s / 3600).toFixed(1)} h` : "–"),
  ));
  root.append(el("h2", {}, "Rows written by stream"));
  root.append(table([{ label: "Stream", get: (r) => r[0] }, { label: "Rows", get: (r) => r[1], fmt: fmt.n }], Object.entries(rec.rows_written ?? {})));
}

const RENDER = { overview, live, paper, lab, calibration, trades, health };

async function render() {
  const root = document.getElementById("content");
  document.getElementById("page-title").textContent = PAGES[page];
  root.replaceChildren(el("div", { class: "empty" }, "Loading…"));
  const frag = el("div");
  await RENDER[page](frag);
  root.replaceChildren(...frag.childNodes);
  const snap = cache.snapshot?.v;
  document.getElementById("updated").textContent = snap ? `data ${fmt.ago(snap.received_at)}` : "";
  document.getElementById("side-status").replaceChildren(
    ...["snapshot", "research", "calibration", "trades", "execution"].map((k) =>
      el("div", {}, el("span", { class: `pill ${cache[k]?.v ? "good" : "warn"}` }, k), " ", cache[k]?.v ? fmt.ago(cache[k].v.received_at) : "none")),
  );
  clearInterval(timer);
  if (page === "live" || page === "overview" || page === "health") timer = setInterval(() => { for (const k in cache) delete cache[k]; render(); }, 60_000);
}

document.getElementById("nav").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-page]");
  if (!a) return;
  page = a.dataset.page;
  for (const x of document.querySelectorAll("#nav a")) x.classList.toggle("active", x === a);
  render();
});
document.getElementById("refresh").addEventListener("click", () => { for (const k in cache) delete cache[k]; render(); });
Promise.all(["snapshot", "research", "calibration", "trades", "execution"].map((k) => load(k))).then(render);
