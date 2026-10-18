import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { validBase, exactQuantity, formatOrder, proposalText, plainOutcome, engineLines, clock }
  from "../../extension/view.js";

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
      "proposalText", "validBase", "exactQuantity", "plainOutcome", "engineLines", "clock",
      source.slice(source.indexOf("\n") + 1))(
      { getElementById: element }, chrome, fetch, () => {}, formatOrder, proposalText, validBase, exactQuantity,
      plainOutcome, engineLines, clock,
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
    else if (url.endsWith("/api/data?kind=live")) result = { data: { updated_ms: Date.now(), calls: [
      { ticker: "KXBTCD-26OCT1720-T81999.99", series: "KXBTCD", seconds_left: 900 },
      { ticker: "KXBTC15M-26OCT171945-45", series: "KXBTC15M", seconds_left: 300 },
    ] } };
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
  // the ticker box follows the current BTC 15-minute market, no typing needed
  assert.equal(popup.element("ticker").value, "KXBTC15M-26OCT171945-45");
  assert.match(popup.element("ticker-hint").textContent, /Current BTC 15-minute market, closes/);
  assert.equal(popup.element("review").disabled, true);  // paused
  assert.equal(popup.element("resume").disabled, false);
  await popup.element("resume").listeners.click();  // one click, no checkbox
  assert.equal(popup.element("review").disabled, false);
  await popup.element("review").listeners.click();
  assert.equal(sent.filter((item) => item.url.endsWith("action=confirm")).length, 0);  // proposal only
  assert.equal(popup.element("confirm").disabled, false);
  assert.match(popup.element("confirm").textContent, /^Buy 1.00 YES at \$0.4000/);
  await popup.element("confirm").listeners.click();  // one Buy click sends it
  assert.equal(sent.find((item) => item.url.endsWith("/api/jev-proposal")).body.ticker, "KXBTC15M-26OCT171945-45");
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

test("popup saves money caps and switches autopilot with explicit confirmation", async () => {
  let autopilot = false;
  let limits = { purchase_spend_cents: 500, open_exposure_cents: 300, realized_loss_cents: 300,
    order_debit_cents: 100, max_contracts: 1 };
  const sent = [];
  const fetch = async (url, options) => {
    const body = options.body && JSON.parse(options.body);
    sent.push({ url, body });
    let result;
    if (url.endsWith("action=status")) result = {
      configured: true, enabled: true, paused: false, environment: "production",
      account_updated_ms: Date.now(), remaining_purchase_budget: "5.0", open_exposure: "0",
      realized_loss: "0", positions: [], orders: [], limits, autopilot, autopilot_available: true,
      autopilot_last: autopilot ? [{ at_ms: Date.now(), ticker: "KXBTC15M-FIXTURE",
        jev: { action: "hold", confidence: 0.4 }, outcome: "Jev chose hold: no order",
        engine: { seconds_left: 425, fair_up: 0.62, up_price: 0.6, down_price: 0.41, edge_up: 0.004,
          edge_down: -0.04, edge_needed: 0.03, failed_gates: [], share_bought_up_5m: 0.7,
          index_change_5m_bps: -12 } }] : [],
    };
    else if (url.endsWith("/api/data?kind=live")) result = { data: { updated_ms: Date.now(), calls: [] } };
    else if (url.endsWith("action=limits")) { limits = body.limits; result = { limits }; }
    else if (url.endsWith("action=autopilot")) { autopilot = body.enabled; result = { autopilot }; }
    else throw new Error("Unexpected fixture request");
    return new Response(JSON.stringify(result), { headers: { "Content-Type": "application/json" } });
  };
  const storage = { local: { base: "https://fixture-dashboard.test", passcode: "fixture-password" }, session: {} };
  const popup = popupHarness(storage, fetch);
  await popup.open();
  assert.equal(popup.element("cap-loss").value, "3");
  assert.equal(popup.element("autopilot-on").disabled, false);
  assert.equal(popup.element("autopilot-off").disabled, true);

  for (const [id, value] of [["cap-spend", "50"], ["cap-exposure", "15"], ["cap-loss", "15"],
    ["cap-order", "5"], ["cap-contracts", "1"]]) {
    popup.element(id).value = value;
    popup.element(id).listeners.input();
  }
  await popup.element("save-caps").listeners.click();
  const saved = sent.find((item) => item.url.endsWith("action=limits")).body;
  assert.deepEqual(saved, { confirmed: true, limits: { purchase_spend_cents: 5000,
    open_exposure_cents: 1500, realized_loss_cents: 1500, order_debit_cents: 500, max_contracts: 1 } });

  popup.element("cap-loss").value = "2.5";
  await popup.element("save-caps").listeners.click();
  assert.match(popup.element("message").textContent, /whole numbers/);
  assert.equal(sent.filter((item) => item.url.endsWith("action=limits")).length, 1);

  await popup.element("autopilot-on").listeners.click();
  assert.deepEqual(sent.find((item) => item.url.endsWith("action=autopilot")).body,
    { confirmed: true, enabled: true });
  assert.match(popup.element("autopilot-state").textContent, /^ON: Jev trades/);
  assert.match(popup.element("autopilot-log").textContent, /Jev said hold \(40% sure\)\. No order\./);
  assert.equal(popup.element("state").textContent, "AUTOPILOT ON");
  assert.match(popup.element("now-market").textContent, /^KXBTC15M-FIXTURE · closes in 7:0\d · checked/);
  assert.match(popup.element("now-engine").textContent, /UP costs 60¢ · engine says 62% · edge \+0\.4¢/);
  assert.match(popup.element("now-engine").textContent, /DOWN costs 41¢ · engine says 38% · edge −4\.0¢/);
  assert.match(popup.element("now-engine").textContent, /at least \+3\.0¢/);
  assert.match(popup.element("now-engine").textContent, /70% of contracts bought UP · BTC -0\.12% in 5 min/);
  assert.equal(popup.element("spent").textContent, "$0.00 of $50.00");
  assert.equal(popup.element("bar-loss").max, 15);
  assert.equal(popup.element("autopilot-off").disabled, false);
  await popup.element("autopilot-off").listeners.click();
  assert.match(popup.element("autopilot-state").textContent, /^Off/);
});

test("autopilot outcomes read as plain sentences", () => {
  assert.equal(plainOutcome({ outcome: "refused: deterministic data-quality gates failed: 15 min of index history (warm-up)" }),
    "Waiting for good data: 15 min of index history (warm-up). No order.");
  assert.match(plainOutcome({ outcome: "no trade: engine sees no edge after fees" }), /neither side is cheap enough/);
  assert.match(plainOutcome({ outcome: "already holding this market: autopilot never adds" }), /never buys more/);
  assert.match(plainOutcome({ outcome: "error: HTTPStatusError" }), /usable answer from Jev \(HTTPStatusError\)/);
  assert.match(plainOutcome({ outcome: "refused: market closed or outside model horizon" }), /^Blocked by a safety check: market closed/);
  assert.equal(plainOutcome({ outcome: "order executed", jev: { action: "enter_yes", confidence: 0.81 },
    order: { intent: { action: "buy", side: "yes" }, status: "executed", filled_quantity: "1.00",
      fill_cost: "0.4000", fees: "0.0200" } }),
  "Jev said enter yes (81% sure). Bought UP: filled 1.00 for $0.4000 + $0.0200 fees (executed).");
  assert.deepEqual(engineLines(undefined), []);
  assert.equal(clock(65.4), "1:05");
  assert.equal(clock(-3), "0:00");
});
