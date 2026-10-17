import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { validBase, exactQuantity, formatOrder, proposalText } from "../../extension/view.js";

test("extension validates origin and exact quantities", () => {
  assert.equal(validBase("https://dashboard.test"), "https://dashboard.test");
  for (const value of ["http://dashboard.test", "https://user:pass@dashboard.test", "https://dashboard.test/path"]) {
    assert.throws(() => validBase(value));
  }
  assert.equal(exactQuantity("1"), "1.00");
  assert.equal(exactQuantity("0.37"), "0.37");
  for (const value of ["-1", "0", "1.001", "NaN", "1e2"]) assert.throws(() => exactQuantity(value));
});

test("actual order state is distinct from proposal and unknown outcomes", () => {
  assert.match(formatOrder({ status: "unknown" }), /may have reached Kalshi/);
  assert.match(formatOrder({ status: "rejected" }), /rejected/);
  assert.match(formatOrder({ status: "executed", broker_acknowledged: true,
    filled_quantity: "0.37", fill_cost: ".148", fees: ".005" }), /Filled 0.37/);
  const text = proposalText({
    intent: { ticker: "FIXTURE", action: "buy", side: "yes", quantity: "1.00", limit_price: "0.4000" },
    maximum_debit: ".4276", fee_reserve: ".0276", estimated_settlement_probability: .7,
    estimated_edge_after_reserved_fees: .2724, decision: { confidence: .9 }, expires_ms: Date.now() + 1000,
  });
  assert.match(text, /Engine settlement estimate: 70.0%/);
  assert.match(text, /Jev decision confidence: 90.0% \(not settlement probability\)/);
});

test("extension reads Kalshi pages only and has no broker/gateway credential surface", async () => {
  const manifest = JSON.parse(await readFile(new URL("../../extension/manifest.json", import.meta.url)));
  assert.deepEqual(manifest.permissions, ["storage"]);
  assert.deepEqual(manifest.content_scripts, [{
    matches: ["https://kalshi.com/*", "https://*.kalshi.com/*"],
    js: ["kalshi-core.js", "kalshi.js"], run_at: "document_idle",
  }]);
  const page = await readFile(new URL("../../extension/kalshi.js", import.meta.url), "utf8");
  // the page panel never scripts Kalshi's own UI or reaches the order route
  assert.ok(!/\.click\(|dispatchEvent|querySelector|innerHTML|execution\?action|confirmation_token/.test(page));
  assert.match(page, /attachShadow\(\{ mode: "closed" \}\)/);
  const source = await readFile(new URL("../../extension/popup.js", import.meta.url), "utf8");
  assert.ok(!/kalshi\.com|AI_GATEWAY_API_KEY|KALSHI-ACCESS|executeScript|\.click\(/.test(source));
  assert.match(source, /confirmation_token: proposal.confirmation_token/);
  assert.match(source, /idempotency_key: idempotencyKey/);
});

function popupHarness(storage, fetch) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "", checked: false, hidden: false, disabled: false, textContent: "",
      listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; },
    });
    return elements.get(id);
  };
  element("base").value = "https://fixture-dashboard.test";
  element("quantity").value = "1.00";
  element("ticker").value = "KXBTC15M-FIXTURE";
  const area = (name) => ({
    get: async (keys) => Object.fromEntries([keys].flat().map((k) => [k, storage[name][k]])),
    set: async (data) => Object.assign(storage[name], data),
    remove: async (key) => { delete storage[name][key]; },
  });
  const chrome = { permissions: { request: async () => true },
    storage: { local: area("local"), session: area("session") } };
  return { element, open: async () => {
    const source = await readFile(new URL("../../extension/popup.js", import.meta.url), "utf8");
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    await new AsyncFunction("document", "chrome", "fetch", "setInterval", "formatOrder",
      "proposalText", "validBase", "exactQuantity", source.slice(source.indexOf("\n") + 1))(
      { getElementById: element }, chrome, fetch, () => {}, formatOrder, proposalText, validBase, exactQuantity,
    );
  } };
}

test("popup remembers the password, auto-connects, resumes and buys in one click each", async () => {
  let paused = true;
  const sent = [];
  const proposal = {
    status: "confirmation_required", submitted: false, proposal_id: "p".repeat(48),
    confirmation_token: "t".repeat(43), expires_ms: Date.now() + 15000,
    intent: { ticker: "KXBTC15M-FIXTURE", action: "buy", side: "yes",
      quantity: "1.00", limit_price: "0.4000" },
    maximum_debit: ".4276", fee_reserve: ".0276",
    estimated_settlement_probability: .7, estimated_edge_after_reserved_fees: .2724,
    decision: { confidence: .9 },
  };
  const fetch = async (url, options) => {
    const body = options.body && JSON.parse(options.body);
    sent.push({ url, body, auth: options.headers.Authorization });
    let result;
    if (url.endsWith("action=status")) result = {
      configured: true, enabled: true, paused, environment: "production",
      account_updated_ms: Date.now(), remaining_purchase_budget: "5.0", open_exposure: "0",
      realized_loss: "0", positions: [], orders: [],
      limits: { purchase_spend_cents: 500, open_exposure_cents: 300, realized_loss_cents: 300, order_debit_cents: 100 },
    };
    else if (url.endsWith("action=resume")) { paused = false; result = {}; }
    else if (url.endsWith("/api/jev-proposal")) result = proposal;
    else if (url.endsWith("action=confirm")) result = {
      status: "canceled", broker_acknowledged: true, order_id: "fixture-order",
      intent: proposal.intent, filled_quantity: "0.37", fill_cost: ".148", fees: ".005",
    };
    else throw new Error("Unexpected fixture request");
    return new Response(JSON.stringify(result), { headers: { "Content-Type": "application/json" } });
  };
  const storage = { local: {}, session: {} };

  // first open: one-time setup
  const first = popupHarness(storage, fetch);
  await first.open();
  assert.equal(sent.length, 0);  // nothing saved yet, so no auto-connect
  assert.equal(first.element("setup").hidden, false);
  first.element("passcode").value = "fixture-password";
  await first.element("connect").listeners.click();
  assert.deepEqual(storage.local, { base: "https://fixture-dashboard.test", passcode: "fixture-password" });
  assert.equal(first.element("passcode").value, "");
  assert.equal(first.element("setup").hidden, true);

  // next open: connects by itself with the saved password
  sent.length = 0;
  const popup = popupHarness(storage, fetch);
  await popup.open();
  assert.ok(sent[0].url.endsWith("action=status"));
  assert.equal(sent[0].auth, `Basic ${btoa("edgebook:fixture-password")}`);
  assert.equal(popup.element("review").disabled, true);  // paused
  assert.equal(popup.element("resume").disabled, false);
  await popup.element("resume").listeners.click();  // one click, no checkbox
  assert.equal(popup.element("review").disabled, false);
  await popup.element("review").listeners.click();
  assert.equal(sent.filter((item) => item.url.endsWith("action=confirm")).length, 0);  // proposal only
  assert.equal(popup.element("confirm").disabled, false);
  assert.match(popup.element("confirm").textContent, /^Buy 1.00 YES at \$0.4000/);
  await popup.element("confirm").listeners.click();  // one Buy click sends it
  const confirmation = sent.find((item) => item.url.endsWith("action=confirm")).body;
  assert.deepEqual(confirmation.intent, proposal.intent);
  assert.equal(confirmation.confirmed, true);
  assert.equal(confirmation.confirmation_token, proposal.confirmation_token);
  assert.ok(confirmation.idempotency_key);
  assert.match(popup.element("orders").textContent, /Filled 0.37/);
  assert.equal(popup.element("confirm").disabled, true);
  assert.equal(storage.session.pending, undefined);

  await popup.element("forget").listeners.click();
  assert.deepEqual(storage.local, { base: "https://fixture-dashboard.test" });
});
