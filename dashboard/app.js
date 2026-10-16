// edgebook dashboard. Page structure (sidebar router, metric-card rows,
// refresh control, Plotly panels) adapted from ryanfrigo/kalshi-ai-trading-bot
// scripts/trading_dashboard.py, MIT License; see THIRD_PARTY_LICENSES.md.
"use strict";

const PAGES = {
  overview: "Overview",
  live: "Live Markets",
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
  const body = rows.map((r) =>
    el("tr", {}, cols.map((c) => {
      const v = c.get(r);
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

// ---------- pages ----------
async function overview(root) {
  const [snap, research] = await Promise.all([load("snapshot"), load("research")]);
  const s = snap?.data;
  const rec = s?.recorder ?? {};
  const sig15 = s?.signals?.KXBTC15M?.[0];
  const best = sig15 ? Math.max(sig15.edge_yes, sig15.edge_no) : null;
  root.append(
    el("div", { class: "cards" },
      card("BTC (Coinbase)", fmt.usd(s?.spot), `snapshot ${fmt.ago(snap?.received_at)}`),
      card("15-min market fair", sig15 ? fmt.p(sig15.fair) : "–", sig15 ? `bid ${fmt.p(sig15.bid)} / ask ${fmt.p(sig15.ask)}` : "no open market"),
      card("Best 15-min edge", fmt.c(best), "after taker fee", best == null ? "" : best > 0 ? "good" : "bad"),
      card("Recorder", rec.msgs_per_s != null ? `${Math.round(rec.msgs_per_s)}/s` : "–", `${fmt.n(rec.markets)} markets, ${fmt.n(rec.gaps)} gaps`, rec.gaps ? "bad" : "good"),
    ),
  );
  const chart = el("div", { class: "panel chart" });
  root.append(el("h2", {}, "BTC, last few hours"), chart);
  const series = s?.spot_series ?? [];
  if (series.length) {
    plot(chart, [{ x: series.map((p) => new Date(p[0])), y: series.map((p) => p[1]), type: "scatter", mode: "lines", line: { color: css("--accent"), width: 2 }, name: "BTC-USD" }], { yaxis: { gridcolor: css("--border"), tickprefix: "$" } });
  } else chart.replaceWith(empty("No snapshot yet: the Fly recorder publishes one every minute."));

  root.append(el("h2", {}, "Research verdict (out-of-sample, after fees)"));
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
  const snap = await load("snapshot", true);
  const sigs = snap?.data?.signals ?? {};
  root.append(el("p", { class: "muted" }, `Snapshot ${fmt.ago(snap?.received_at)}. Fair value = HAR volatility + Student-t (ν=4) pricer of the 60-second settlement average. Edges are per contract after Kalshi's taker fee.`));
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

async function lab(root) {
  const research = await load("research");
  const series = research?.data?.series ?? {};
  if (!Object.keys(series).length) return root.append(empty("No research published yet: run `edgebook research --days 30 --publish`."));
  root.append(el("p", { class: "muted" }, `Walk-forward: each day is predicted by a model fit only on earlier days. The last ${research.data.holdout_days} days are a separate holdout. Generated ${fmt.ago(research.data.generated)} on ${research.data.days} days of history.`));
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
  if (!list.length) return root.append(empty("No trades yet. `edgebook trade-once --execute` logs every order here."));
  root.append(table([
    { label: "Time", get: (r) => new Date(r.ts).toLocaleString() },
    { label: "Env", get: (r) => r.env },
    { label: "Market", get: (r) => r.order.ticker },
    { label: "Side", get: (r) => (r.order.side === "bid" ? "YES" : "NO") },
    { label: "Price", get: (r) => (r.order.side === "bid" ? r.order.price : (1 - r.order.price).toFixed(4)) },
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

const RENDER = { overview, live, lab, calibration, trades, health };

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
    ...["snapshot", "research", "calibration", "trades"].map((k) =>
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
Promise.all(["snapshot", "research", "calibration", "trades"].map((k) => load(k))).then(render);
