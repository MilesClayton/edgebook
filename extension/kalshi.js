// Edgebook panel on kalshi.com market pages.
//
// Reads only the page URL to know which market you are looking at, then shows Edgebook's
// live call for it and lets you ask Jev for a bounded decision. It never reads or touches
// Kalshi's own page elements and cannot place orders: a Jev proposal is handed to the
// toolbar popup, where it is only sent if you click Buy.
"use strict";
(() => {
  const K = globalThis.EdgebookKalshi;
  if (!K || document.getElementById("edgebook-panel-host")) return;

  const host = document.createElement("div");
  host.id = "edgebook-panel-host";
  const root = host.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  style.textContent = `
    :host { all: initial; }
    .panel { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; width: 360px;
      max-height: 70vh; overflow: auto; font: 12px/1.45 system-ui, sans-serif; color: #e9edf5;
      background: #0b1220; border: 1px solid #29594f; border-radius: 12px; padding: 12px;
      box-shadow: 0 8px 30px rgba(0,0,0,.45); }
    header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
    header strong { letter-spacing: .14em; flex: 1; }
    .pill { font-size: 10px; padding: 2px 6px; border-radius: 99px; background: #0c382f; color: #8ae4c5; }
    .pill.bad { background: #3b1d31; color: #ff8b9e; }
    button { font: inherit; border: 0; border-radius: 6px; padding: 5px 9px; cursor: pointer;
      background: #8ae4c5; color: #09231a; font-weight: 650; }
    button.ghost { background: #23344e; color: #dbe5f5; }
    button:disabled { opacity: .5; cursor: not-allowed; }
    .row { border-top: 1px solid #283248; padding: 8px 0; }
    .top { display: flex; justify-content: space-between; gap: 8px; font-weight: 650; }
    .act { font-size: 11px; padding: 1px 7px; border-radius: 6px; background: #1a2a42; color: #9cacc4; }
    .act.go { background: #8ae4c5; color: #09231a; }
    .muted { color: #92a4be; font-size: 11px; }
    .ask { display: flex; gap: 6px; margin-top: 6px; align-items: center; }
    input { width: 64px; background: #111e32; color: #eef4ff; border: 1px solid #34445f;
      border-radius: 6px; padding: 4px 6px; font: inherit; }
    .result { margin-top: 6px; white-space: pre-wrap; border-left: 3px solid #75caae; padding-left: 8px; }
  `;
  const panel = document.createElement("section");
  panel.className = "panel";
  const header = document.createElement("header");
  const title = document.createElement("strong");
  title.textContent = "EDGEBOOK";
  const age = document.createElement("span");
  age.className = "pill";
  const toggle = document.createElement("button");
  toggle.className = "ghost";
  toggle.textContent = "–";
  toggle.title = "Collapse";
  header.append(title, age, toggle);
  const body = document.createElement("div");
  panel.append(header, body);
  root.append(style, panel);
  document.documentElement.append(host);

  let collapsed = false;
  let lastHref = "";
  const results = new Map(); // ticker -> last Jev message, kept across refreshes

  toggle.addEventListener("click", () => {
    collapsed = !collapsed;
    body.hidden = collapsed;
    toggle.textContent = collapsed ? "+" : "–";
    if (!collapsed) refresh();
  });

  function line(text, cls) {
    const el = document.createElement("div");
    if (cls) el.className = cls;
    el.textContent = text;
    return el;
  }

  async function ask(ticker, quantity, button) {
    button.disabled = true;
    results.set(ticker, "Asking Jev (checks prices, account and paper proof first)…");
    render(lastLive, lastRef);
    try {
      const reply = await chrome.runtime.sendMessage({ type: "edgebook:ask-jev", ticker, quantity });
      if (!reply || !reply.ok) throw new Error((reply && reply.error) || "No reply from Edgebook.");
      results.set(ticker, reply.proposal
        ? `Proposal ready: ${reply.summary}\nOpen the Edgebook toolbar popup and click Buy before ${new Date(reply.proposal.expires_ms).toLocaleTimeString()} to send it. Nothing has been sent.`
        : `Jev chose ${String(reply.status).toUpperCase()}. No order.`);
    } catch (error) {
      results.set(ticker, error.message);
    }
    render(lastLive, lastRef);
  }

  let lastLive = null;
  let lastRef = null;

  function render(live, ref) {
    body.replaceChildren();
    if (!ref) return;
    if (!live) {
      body.append(line("Open the Edgebook toolbar popup once and connect.", "muted"));
      return;
    }
    const calls = K.callsFor(live, ref);
    const longshots = K.longshotsFor(live, ref);
    if (!calls.length && !longshots.length) {
      body.append(line(`Edgebook isn't tracking ${ref.ticker || ref.event || ref.series} right now (it follows the open 15-min market and the strikes nearest BTC for the next hourly close).`, "muted"));
    }
    for (const call of calls) {
      const d = K.describe(call);
      const row = document.createElement("div");
      row.className = "row";
      const top = document.createElement("div");
      top.className = "top";
      const act = line(d.action, `act${/^(BUY|BID|SELL)/.test(d.action) ? " go" : ""}`);
      top.append(line(d.label), act);
      row.append(top, line(d.odds, "muted"), line(d.plan, "muted"), line(`Best expected profit ${d.edge} · ${d.reason}`, "muted"));
      const form = document.createElement("div");
      form.className = "ask";
      const qty = document.createElement("input");
      qty.type = "number";
      qty.min = "0.01";
      qty.step = "0.01";
      qty.value = "1";
      qty.title = "Contracts";
      const button = document.createElement("button");
      button.textContent = "Ask Jev";
      button.title = "Ask Jev for a bounded decision on this market. Creates an unsubmitted proposal at most.";
      button.addEventListener("click", () => ask(call.ticker, qty.value, button));
      form.append(qty, button);
      row.append(form);
      if (results.has(call.ticker)) row.append(line(results.get(call.ticker), "result"));
      body.append(row);
    }
    for (const o of longshots) {
      const long = o.side === "yes" ? "DOWN" : "UP";
      body.append(line(`Long-shot seller (paper): bidding ${o.side === "yes" ? "UP" : "DOWN"} at ${K.price(o.price)} for ${Math.round(o.contracts).toLocaleString("en-US")} contracts, i.e. selling the ${long} long shot at ${K.price(1 - o.price)}.`, "row muted"));
    }
    body.append(line("Paper-trading model output, not advice. Orders only from the popup when you click Buy.", "muted"));
  }

  async function refresh() {
    const ref = K.locate(location.href);
    host.hidden = !ref;
    lastRef = ref;
    if (!ref || collapsed) return;
    let reply = null;
    try {
      reply = await chrome.runtime.sendMessage({ type: "edgebook:live" });
    } catch {
      reply = null; // extension reloaded or worker asleep; try again next tick
    }
    const live = reply && reply.ok ? reply.live : null;
    lastLive = live;
    const stale = !live || Date.now() - live.updated_ms > 15000;
    age.className = `pill${stale ? " bad" : ""}`;
    age.textContent = live ? (stale ? "STALE" : "LIVE") : "OFFLINE";
    age.title = reply && !reply.ok ? reply.error : "";
    render(live, ref);
  }

  setInterval(() => {
    if (location.href !== lastHref) {
      lastHref = location.href;
      results.clear();
      refresh();
    }
  }, 1000);
  setInterval(refresh, 3000);
})();
