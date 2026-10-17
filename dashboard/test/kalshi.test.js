import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

await import("../../extension/kalshi-core.js");
const K = globalThis.EdgebookKalshi;

const LIVE = {
  updated_ms: Date.now(),
  calls: [
    { ticker: "KXBTCD-26OCT1717-T81999.99", series: "KXBTCD", strike: 81999.99, action: "NO TRADE",
      reason: "Price is fair.", model_up: 0.6, market_up: 0.55, fair_up: 0.56, up_price: 0.56,
      down_price: 0.45, buy_up_below: 0.5, buy_down_below: 0.39, edge_up: -0.02, edge_down: -0.03 },
    { ticker: "KXBTCD-26OCT1717-T82099.99", series: "KXBTCD", strike: 82099.99, action: "BUY DOWN",
      reason: "Fair value 70% for DOWN.", model_up: 0.2, market_up: 0.35, fair_up: 0.3, up_price: 0.36,
      down_price: 0.65, buy_up_below: 0.25, buy_down_below: 0.66, edge_up: -0.08, edge_down: 0.04 },
    { ticker: "KXBTC15M-26OCT171715-15", series: "KXBTC15M", strike: 82010.5, action: "NO TRADE",
      reason: "Waiting: 15 min of index history (warm-up).", model_up: null, market_up: 0.5 },
  ],
  longshot_orders: [{ ticker: "KXBTCD-26OCT1717-T82499.99", side: "no", price: 0.96, contracts: 2600 }],
};

test("locates series, event and strike from Kalshi URLs", () => {
  assert.deepEqual(K.locate("https://kalshi.com/markets/kxbtcd/bitcoin-price-abovebelow/kxbtcd-26oct1717"),
    { series: "KXBTCD", event: "KXBTCD-26OCT1717", ticker: null });
  assert.deepEqual(K.locate("https://kalshi.com/markets/kxbtcd/x/kxbtcd-26oct1717-t81999.99"),
    { series: "KXBTCD", event: "KXBTCD-26OCT1717", ticker: "KXBTCD-26OCT1717-T81999.99" });
  assert.deepEqual(K.locate("https://www.kalshi.com/markets/kxbtc15m/btc-15m/kxbtc15m-26oct171715-15?x=1"),
    { series: "KXBTC15M", event: "KXBTC15M-26OCT171715", ticker: "KXBTC15M-26OCT171715-15" });
  assert.deepEqual(K.locate("https://kalshi.com/markets/kxbtcd"), { series: "KXBTCD", event: null, ticker: null });
  for (const href of ["https://kalshi.com/markets/kxeth/eth", "https://example.com/markets/kxbtcd/kxbtcd-26oct1717",
    "not a url", "https://kalshi.com/"]) assert.equal(K.locate(href), null);
});

test("selects the page's calls and long-shot orders", () => {
  const event = K.locate("https://kalshi.com/markets/kxbtcd/x/kxbtcd-26oct1717");
  assert.deepEqual(K.callsFor(LIVE, event).map((c) => c.strike), [82099.99, 81999.99]);
  assert.equal(K.longshotsFor(LIVE, event).length, 1);
  const one = K.locate("https://kalshi.com/markets/kxbtcd/x/kxbtcd-26oct1717-t81999.99");
  assert.deepEqual(K.callsFor(LIVE, one).map((c) => c.strike), [81999.99]);
  assert.equal(K.longshotsFor(LIVE, one).length, 0);
  const series = K.locate("https://kalshi.com/markets/kxbtc15m");
  assert.deepEqual(K.callsFor(LIVE, series).map((c) => c.series), ["KXBTC15M"]);
  assert.deepEqual(K.callsFor(null, series), []);
  const d = K.describe(LIVE.calls[1]);
  assert.equal(d.label, "Above $82,100");
  assert.equal(d.action, "BUY DOWN");
  assert.equal(d.odds, "Model 20% · Kalshi 35% · Fair 30%");
  assert.equal(d.edge, "+4.0¢");
  assert.equal(K.describe(LIVE.calls[2]).action, "WAITING");
  assert.equal(K.describe(LIVE.calls[2]).label, "15-min · start $82,011");
});

function chromeStub(session = {}, local = {}) {
  const area = (store) => ({
    get: async (key) => ({ [key]: store[key] }),
    set: async (data) => Object.assign(store, data),
    remove: async (key) => { delete store[key]; },
  });
  let listener = null;
  return {
    session, local,
    runtime: { id: "ext", onMessage: { addListener: (fn) => { listener = fn; } }, sendMessage: null },
    storage: { local: area(local), session: area(session) },
    dispatch: (message, sender = { id: "ext" }) => new Promise((resolve) => {
      const async = listener(message, sender, resolve);
      if (!async) resolve("ignored");
    }),
  };
}

test("background worker needs opt-in, fetches live data and stores Jev proposals for the popup", async () => {
  const chrome = chromeStub({}, { base: "https://dash.test" });
  const sent = [];
  globalThis.chrome = chrome;
  globalThis.fetch = async (url, options) => {
    sent.push({ url, options });
    const body = url.endsWith("kind=live") ? { data: LIVE }
      : { status: "confirmation_required", proposal_id: "p", confirmation_token: "t", expires_ms: Date.now() + 15000,
        intent: { ticker: "KXBTCD-26OCT1717-T82099.99", action: "buy", side: "no", quantity: "2.00", limit_price: "0.6500" },
        maximum_debit: "1.3", fee_reserve: "0.05", estimated_settlement_probability: 0.7,
        estimated_edge_after_reserved_fees: 0.04, decision: { confidence: 0.8 } };
    return new Response(JSON.stringify(body), { status: 200 });
  };
  await import("../../extension/background.js");
  const off = await chrome.dispatch({ type: "edgebook:live" });
  assert.equal(off.ok, false);
  assert.match(off.error, /Show Edgebook on Kalshi pages/);
  chrome.session.onpage = { base: "https://dash.test", passcode: "secret" };
  const live = await chrome.dispatch({ type: "edgebook:live" });
  assert.equal(live.ok, true);
  assert.equal(live.live.calls.length, 3);
  assert.equal(sent[0].options.headers.Authorization, `Basic ${btoa("edgebook:secret")}`);
  assert.equal(sent[0].options.credentials, "omit");
  const bad = await chrome.dispatch({ type: "edgebook:ask-jev", ticker: "KXETH-1", quantity: "1" });
  assert.equal(bad.ok, false);
  const asked = await chrome.dispatch({ type: "edgebook:ask-jev", ticker: "KXBTCD-26OCT1717-T82099.99", quantity: "2" });
  assert.equal(asked.ok, true);
  assert.match(asked.summary, /BUY NO/);
  assert.deepEqual(JSON.parse(sent.at(-1).options.body), { ticker: "KXBTCD-26OCT1717-T82099.99", quantity: "2.00" });
  assert.equal(chrome.session.pending.base, "https://dash.test");  // the popup picks it up
  assert.ok(chrome.session.pending.idempotencyKey);
  assert.ok(!sent.some((s) => s.url.includes("execution")));  // never confirms or submits
  assert.equal(await chrome.dispatch({ type: "edgebook:live" }, { id: "other" }), "ignored");
  chrome.local.base = "https://other.test";
  assert.match((await chrome.dispatch({ type: "edgebook:live" })).error, /connect again/);
});

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.hidden = false; this.textContent = ""; }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
  attachShadow() { const root = new Node("#shadow"); this.children.push(root); return root; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  text() { return [this.textContent, ...this.children.map((c) => c.text())].join("\n"); }
  find(pred) { return pred(this) ? this : this.children.map((c) => c.find(pred)).find(Boolean); }
}

test("page panel renders the page's calls and hands Ask Jev to the background worker", async () => {
  const html = new Node("html");
  const timers = [];
  const messages = [];
  const document = { documentElement: html, createElement: (tag) => new Node(tag), getElementById: () => null };
  const chrome = { runtime: { sendMessage: async (m) => {
    messages.push(m);
    return m.type === "edgebook:live" ? { ok: true, live: LIVE }
      : { ok: true, proposal: { expires_ms: Date.now() + 15000 }, summary: "BUY NO · KXBTCD-26OCT1717-T82099.99" };
  } } };
  const source = await readFile(new URL("../../extension/kalshi.js", import.meta.url), "utf8");
  new Function("document", "chrome", "location", "setInterval", "globalThis", source)(
    document, chrome, { href: "https://kalshi.com/markets/kxbtcd/x/kxbtcd-26oct1717" },
    (fn) => timers.push(fn), globalThis,
  );
  await timers[0]();  // URL watcher triggers the first refresh
  await new Promise((r) => setTimeout(r, 0));
  const host = html.children[0];
  const text = host.text();
  assert.match(text, /Above \$82,100/);
  assert.match(text, /BUY DOWN/);
  assert.match(text, /Model 20% · Kalshi 35% · Fair 30%/);
  assert.match(text, /selling the UP long shot at 4¢/);
  assert.doesNotMatch(text, /15-min/);  // another event's market is not shown
  const button = host.find((n) => n.tag === "button" && n.textContent === "Ask Jev");
  await button.listeners.click();
  assert.deepEqual(messages.at(-1), { type: "edgebook:ask-jev", ticker: "KXBTCD-26OCT1717-T82099.99", quantity: "1" });
  assert.match(host.text(), /Open the Edgebook toolbar popup to review and confirm/);
});
