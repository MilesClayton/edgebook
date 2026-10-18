// Service worker: the only part that talks to the dashboard on behalf of Kalshi pages.
//
// Uses the dashboard address and password saved by the popup on this machine. A Jev
// proposal is stored as the popup's pending proposal; sending it still takes the popup's
// Buy click.
import { exactQuantity, proposalText, validBase } from "./view.js";

const TICKER = /^(KXBTC15M|KXBTCD)-[A-Z0-9]+-[A-Z0-9.]+$/;
let panelRequest = null;
let panelUntil = 0;

async function access() {
  const { base, passcode } = await chrome.storage.local.get(["base", "passcode"]);
  if (!base || !passcode) throw new Error("Open the Edgebook toolbar popup once and connect.");
  return { base: validBase(base), passcode };
}

async function request(path, body) {
  const { base, passcode } = await access();
  const response = await fetch(base + path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: `Basic ${btoa(`edgebook:${passcode}`)}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    credentials: "omit",
    redirect: "error",
    signal: AbortSignal.timeout(30000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.reason || "Request refused.");
  return { base, data };
}

export async function handle(message) {
  if (message.type === "edgebook:open-controls") {
    await chrome.tabs.create({ url: chrome.runtime.getURL("popup.html") });
    return { ok: true };
  }
  if (message.type === "edgebook:live") {
    // All open market panels share one read. Only presentation fields cross
    // into content scripts; credentials and proposal tokens stay in the worker.
    if (!panelRequest || Date.now() >= panelUntil) {
      panelUntil = Infinity;
      panelRequest = Promise.allSettled([
        request("/api/data?kind=live"), request("/api/execution?action=status"),
      ]).then(([live, status]) => {
        panelUntil = Date.now() + 4000;
        if (live.status === "rejected" && status.status === "rejected") throw live.reason;
        const source = status.status === "fulfilled" ? status.value.data : null;
        const execution = source ? Object.fromEntries([
          "configured", "enabled", "paused", "autopilot", "longshot", "unknown_or_pending",
          "error", "reason", "remaining_purchase_budget", "open_exposure", "realized_pnl",
          "account_updated_ms", "environment", "autopilot_last", "longshot_last",
          "positions", "limits", "resting_orders",
          "auto_recovery", "recovery_checks", "pause_kind",
        ].map((key) => [key, source[key]])) : null;
        return { ok: true, live: live.status === "fulfilled" ? live.value.data.data : null,
          execution, checked_ms: Date.now(),
          execution_error: status.status === "rejected" ? String(status.reason.message) : null };
      }).catch((error) => { panelRequest = null; throw error; });
    }
    return panelRequest;
  }
  if (message.type === "edgebook:ask-jev") {
    if (!TICKER.test(String(message.ticker))) throw new Error("Not an Edgebook BTC market ticker.");
    const { base, data } = await request("/api/jev-proposal", {
      ticker: message.ticker,
      quantity: exactQuantity(String(message.quantity)),
    });
    if (data.status !== "confirmation_required") return { ok: true, status: data.status };
    const pending = { proposal: data, idempotencyKey: crypto.randomUUID(), base };
    await chrome.storage.session.set({ pending });
    return { ok: true, proposal: data, summary: proposalText(data).split("\n")[0] };
  }
  throw new Error("Unknown request.");
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !message || typeof message.type !== "string") return false;
  handle(message).then(sendResponse, (error) => sendResponse({ ok: false, error: error.message }));
  return true; // reply asynchronously
});
