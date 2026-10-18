// Persistent, read-only trading monitor on Kalshi pages. Order authorization
// remains in the extension controls and persistent execution service.
"use strict";
(() => {
  const K = globalThis.EdgebookKalshi;
  if (!K || document.getElementById("edgebook-panel-host")) return;
  const host = document.createElement("div");
  host.id = "edgebook-panel-host";
  const root = host.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  style.textContent = `
    :host{all:initial;color-scheme:dark}
    *{box-sizing:border-box}
    .panel{position:fixed;right:16px;bottom:16px;z-index:2147483647;width:370px;max-width:calc(100vw - 32px);max-height:70vh;overflow:auto;font:13px/1.5 system-ui,sans-serif;color:#edf2f9;background:#0b1220;border:1px solid #314158;border-radius:14px;box-shadow:0 12px 38px #0006}
    header{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid #28364b}
    header strong{flex:1;font-size:14px;letter-spacing:.06em}
    .pill,.act{font-size:10px;font-weight:700;padding:4px 7px;border-radius:20px;background:#213147;color:#c0cee0;white-space:nowrap}
    .good{background:#123a30;color:#91e4c1}.warn{background:#3d301d;color:#f6cf88}.bad{background:#3b1d31;color:#ffadb1}
    button,a{font:inherit;cursor:pointer}button{min-height:36px;padding:6px 10px;border:1px solid #3d526d;border-radius:8px;background:#1b283a;color:#edf2f9;font-weight:600}button:disabled{opacity:.5;cursor:not-allowed}button:hover,a:hover{filter:brightness(1.15)}button:focus-visible,a:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid #b4d8ff;outline-offset:2px}
    .body{padding:14px}.headline{font-size:18px;font-weight:750;letter-spacing:-.3px;margin-bottom:6px}.muted{color:#a4b3c7;font-size:12px}.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin:13px 0}.stat{background:#142033;border-radius:9px;padding:9px}.stat span{display:block;color:#a4b3c7;font-size:10px}.stat strong{font-size:16px;font-variant-numeric:tabular-nums}
    .actions{display:flex;gap:8px;margin:12px 0}.actions a{flex:1;text-align:center;text-decoration:none;min-height:36px;padding:7px 10px;border-radius:8px;background:#91e4c1;color:#092219;font-weight:650}.actions a.secondary{background:#1b283a;color:#edf2f9;border:1px solid #3d526d}
    .section-title{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:#a4b3c7;margin:14px 0 7px}.position,.result{padding:8px 10px;background:#142033;border-radius:8px;font-size:12px;margin:6px 0;white-space:pre-wrap;overflow-wrap:anywhere}.decision{font-size:12px;padding:9px 0;overflow-wrap:anywhere}.row{border-top:1px solid #28364b;padding:10px 0}.top{display:flex;justify-content:space-between;gap:8px;font-weight:650}.act.go{background:#123a30;color:#91e4c1}details{margin-top:12px;border-top:1px solid #28364b;padding-top:10px}summary{cursor:pointer;color:#c0cee0;font-size:12px}.ask{display:flex;gap:8px;align-items:end;margin-top:10px}label{font-size:11px;color:#a4b3c7}input{display:block;width:85px;min-height:36px;background:#111e32;color:#edf2f9;border:1px solid #3d526d;border-radius:8px;padding:6px;font:inherit}.note{padding-top:10px;border-top:1px solid #28364b;margin-top:12px}[hidden]{display:none!important}
  `;
  const panel = document.createElement("section");
  panel.className = "panel";
  const header = document.createElement("header");
  const title = document.createElement("strong");
  title.textContent = "edgebook";
  const stateBadge = document.createElement("span");
  stateBadge.className = "pill warn";
  stateBadge.textContent = "CHECKING";
  const toggle = document.createElement("button");
  toggle.textContent = "−";
  toggle.title = "Collapse monitor";
  const body = document.createElement("div");
  body.className = "body";
  header.append(title, stateBadge, toggle);
  panel.append(header, body);
  root.append(style, panel);
  document.documentElement.append(host);
  let collapsed = false, lastHref = "", refreshing = false;
  let lastLive = null, lastRef = null, lastReply = null;
  let editing = false;
  const results = new Map(), quantities = new Map(), asking = new Set();
  const expanded = new Set();
  const usd = (x) => x == null || !Number.isFinite(Number(x)) ? "—" : `$${Number(x).toFixed(2)}`;

  function line(text, cls) {
    const node = document.createElement("div");
    if (cls) node.className = cls;
    node.textContent = text;
    return node;
  }
  function setCollapsed(value) {
    collapsed = value;
    body.hidden = value;
    toggle.textContent = value ? "+" : "−";
    toggle.title = value ? "Expand monitor" : "Collapse monitor";
    toggle.setAttribute?.("aria-expanded", String(!value));
  }
  toggle.addEventListener("click", () => {
    setCollapsed(!collapsed);
    chrome.storage?.local?.set({ panelCollapsed: collapsed }).catch(() => {});
    if (!collapsed) refresh();
  });
  chrome.storage?.local?.get("panelCollapsed").then((saved) => setCollapsed(Boolean(saved.panelCollapsed))).catch(() => {});

  async function ask(ticker, quantity) {
    if (asking.has(ticker)) return;
    asking.add(ticker);
    results.set(ticker, "Checking prices and asking Jev…");
    render(lastLive, lastRef);
    try {
      const reply = await chrome.runtime.sendMessage({ type: "edgebook:ask-jev", ticker, quantity });
      if (!reply?.ok) throw new Error(reply?.error || "No reply from Edgebook.");
      results.set(ticker, reply.proposal
        ? `Manual proposal ready: ${reply.summary}\nOpen Controls and confirm Buy before ${new Date(reply.proposal.expires_ms).toLocaleTimeString()}. This manual review sent no order.`
        : `Jev chose ${String(reply.status).toUpperCase()}. This manual review sent no order.`);
    } catch (error) { results.set(ticker, error.message); }
    finally { asking.delete(ticker); render(lastLive, lastRef); }
  }

  function render(live, ref) {
    if (editing) return;
    body.replaceChildren();
    if (!ref) return;
    const execution = lastReply?.execution;
    const state = K.executionState(execution, lastReply?.checked_ms);
    stateBadge.className = `pill ${state.tone}`;
    stateBadge.textContent = state.label;
    body.append(line(state.title, "headline"), line(state.detail, "muted"));
    if (lastReply?.execution_error) body.append(line(`Account check failed: ${lastReply.execution_error}`, "result"));
    if (execution?.configured) {
      const stats = document.createElement("div");
      stats.className = "stats";
      for (const [label, value] of [["Budget room", execution.remaining_purchase_budget], ["Open risk", execution.open_exposure], ["Realized P&L", execution.realized_pnl]]) {
        const stat = document.createElement("div"); stat.className = "stat";
        const caption = document.createElement("span"); caption.textContent = label;
        const amount = document.createElement("strong"); amount.textContent = usd(value);
        stat.append(caption, amount); stats.append(stat);
      }
      body.append(stats);
    }
    const actions = document.createElement("div"); actions.className = "actions";
    const controls = document.createElement("a");
    controls.textContent = "Trading controls";
    controls.href = chrome.runtime.getURL?.("popup.html") || "#";
    controls.target = "_blank"; controls.rel = "noopener";
    controls.addEventListener("click", async (event) => {
      event.preventDefault();
      try {
        const reply = await chrome.runtime.sendMessage({ type: "edgebook:open-controls" });
        if (!reply?.ok) throw new Error("Could not open controls");
      } catch { controls.textContent = "Use the Edgebook toolbar button"; }
    });
    const dashboard = document.createElement("a"); dashboard.className = "secondary";
    dashboard.textContent = "All real positions ↗";
    dashboard.href = "https://dashboard-ten-murex-53tctwc6fk.vercel.app/#positions";
    dashboard.target = "_blank"; dashboard.rel = "noopener";
    actions.append(controls, dashboard); body.append(actions);
    const notes = [...(execution?.autopilot_last || []), ...(execution?.longshot_last || [])].sort((a,b) => b.at_ms - a.at_ms);
    const latest = notes.find((n) => K.callsFor({ calls: [{ ticker: n.ticker }] }, ref).length);
    if (latest) {
      const age = Math.max(0, Math.round((Date.now() - latest.at_ms) / 1000));
      body.append(line(`Last engine check · ${age}s ago`, "section-title"), line(latest.outcome, "decision"));
    }
    if (execution?.configured) {
      const positions = execution.positions || [];
      body.append(line(`Real positions · ${positions.length} across all markets`, "section-title"));
      const onPage = positions.filter((p) => K.callsFor({ calls: [{ ticker: p.ticker }] }, ref).length);
      for (const position of onPage) body.append(line(`${position.side === "yes" ? "UP" : "DOWN"} · ${(position.units / 100).toFixed(2)} contracts · cost ${usd(position.cost / 1e8)}`, "position"));
      if (!onPage.length) body.append(line(positions.length ? "No routed position on this page. View all positions on the dashboard." : "No routed positions open.", "muted"));
    }
    const fresh = live && Date.now() - live.updated_ms <= 15000;
    body.append(line(fresh ? "Market data · live" : "Market data · unavailable or stale", "section-title"));
    if (!live) { body.append(line(lastReply?.error || "Open Trading controls to connect.", "muted")); return; }
    const calls = K.callsFor(live, ref);
    if (!calls.length) body.append(line("This market is not in the current engine window. The server continues managing its supported markets.", "muted"));
    for (const call of calls) {
      const d = K.describe(call);
      const row = document.createElement("div"); row.className = "row";
      const top = document.createElement("div"); top.className = "top";
      top.append(line(d.label), line(fresh ? d.action : "STALE", `act${fresh && /^(BUY|BID|SELL)/.test(d.action) ? " go" : ""}`));
      row.append(top, line(d.odds, "muted"));
      const detail = document.createElement("details");
      detail.open = expanded.has(call.ticker);
      detail.addEventListener("toggle", () => detail.open ? expanded.add(call.ticker) : expanded.delete(call.ticker));
      const summary = document.createElement("summary"); summary.textContent = "Forecast & manual review";
      detail.append(summary, line(d.plan, "muted"), line(`Estimated edge ${d.edge} · ${d.reason}`, "muted"), line("This forecast is separate from the automatic trading state above.", "muted"));
      const form = document.createElement("div"); form.className = "ask";
      const label = document.createElement("label"); label.textContent = "Contracts";
      const qty = document.createElement("input");
      qty.type = "number"; qty.min = "0.01"; qty.step = "0.01";
      qty.value = quantities.get(call.ticker) || "1"; qty.title = "Contracts";
      qty.addEventListener("input", () => quantities.set(call.ticker, qty.value));
      qty.addEventListener("focus", () => { editing = true; });
      qty.addEventListener("blur", () => { editing = false; });
      label.append(qty);
      const button = document.createElement("button"); button.textContent = "Ask Jev";
      button.title = "Manual review only; does not submit an order.";
      button.disabled = !fresh || !execution?.configured || !execution.enabled || execution.paused || execution.unknown_or_pending || asking.has(call.ticker);
      button.addEventListener("click", () => ask(call.ticker, qty.value));
      form.append(label, button); detail.append(form); row.append(detail);
      if (results.has(call.ticker)) row.append(line(results.get(call.ticker), "result"));
      body.append(row);
    }
    for (const order of K.longshotsFor(live, ref)) body.append(line(`Paper long-shot bid · ${order.side === "yes" ? "UP" : "DOWN"} at ${K.price(order.price)} · ${Math.round(order.contracts).toLocaleString("en-US")} simulated contracts.`, "muted"));
    body.append(line(execution?.autopilot || execution?.longshot
      ? "Automatic orders are controlled by the saved budget. Manual reviews are separate."
      : "Model forecasts are estimates. Trading controls shows whether real execution is active.", "note muted"));
  }

  async function refresh() {
    const ref = K.locate(location.href);
    host.hidden = !ref;
    lastRef = ref;
    if (!ref || refreshing) return;
    refreshing = true;
    try { lastReply = await chrome.runtime.sendMessage({ type: "edgebook:live" }); }
    catch { lastReply = { ok: false, error: "Extension connection lost. Refresh this Kalshi page after reloading Edgebook." }; }
    finally { refreshing = false; }
    lastLive = lastReply?.ok ? lastReply.live : null;
    render(lastLive, ref);
  }
  setInterval(() => {
    if (location.href !== lastHref) {
      lastHref = location.href;
      results.clear(); quantities.clear(); refresh();
    }
  }, 1000);
  setInterval(refresh, 4000);
})();
