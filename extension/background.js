// Service worker: the only part that talks to the dashboard on behalf of Kalshi pages.
//
// Uses the dashboard origin saved by the popup and a passcode kept in Chrome *session*
// storage (memory only, cleared when Chrome closes), and only if you opted in from the
// popup. A Jev proposal is stored as the popup's pending proposal; confirming it still
// requires the popup's exact-terms checkbox.
import { exactQuantity, proposalText, validBase } from "./view.js";

const TICKER = /^(KXBTC15M|KXBTCD)-[A-Z0-9]+-[A-Z0-9.]+$/;

async function access() {
  const { base } = await chrome.storage.local.get("base");
  const { onpage } = await chrome.storage.session.get("onpage");
  if (!base || !onpage || !onpage.passcode) {
    throw new Error("Open the Edgebook popup, connect, and tick “Show Edgebook on Kalshi pages”.");
  }
  if (validBase(base) !== onpage.base) throw new Error("Dashboard changed; connect again in the popup.");
  return { base: validBase(base), passcode: onpage.passcode };
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
  if (message.type === "edgebook:live") {
    const { data } = await request("/api/data?kind=live");
    return { ok: true, live: data.data };
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
