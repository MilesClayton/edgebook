import { formatOrder, proposalText, validBase, exactQuantity, plainOutcome, engineLines, clock } from "./view.js";

// Personal extension: the dashboard address and password are entered once and remembered on
// this machine (chrome.storage.local), and the popup connects by itself when opened. The
// server still requires the password; the extension just supplies it.
const element = (id) => document.getElementById(id);
let proposal = null;
let idempotencyKey = null;
let ready = false;
let busy = false;
let status = null;
let saved = { base: "", passcode: "" };
let tickerTyped = false; // you typed a ticker yourself: don't overwrite it
let amountMode = "contracts";
let budgetEdited = false;
let strategyEdited = false;
let stopping = false;

for (const name of ["live", "help"]) element(`tab-${name}`).addEventListener("click", () => {
  for (const tab of ["live", "help"]) {
    element(`page-${tab}`).hidden = tab !== name;
    element(`tab-${tab}`).className = tab === name ? "active" : "";
    element(`tab-${tab}`).setAttribute?.("aria-pressed", String(tab === name));
  }
});

async function request(path, body) {
  const base = validBase(saved.base);
  if (!saved.passcode) throw new Error("Enter the dashboard password once to connect.");
  const response = await fetch(base + path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: `Basic ${btoa(`edgebook:${saved.passcode}`)}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    credentials: "omit",
    redirect: "error",
    signal: AbortSignal.timeout(30000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.reason || "Request refused.");
  return data;
}

function controls() {
  for (const id of ["connect", "reconcile", "resume", "forget"]) element(id).disabled = busy || stopping;
  element("pause").disabled = stopping || !saved.passcode;
  element("review").disabled = busy || !ready;
  element("confirm").disabled = busy || !ready || !proposal || Date.now() >= proposal.expires_ms;
  element("resume").disabled = busy || !status?.configured || !status?.enabled || !status?.paused;
  element("autopilot-on").disabled = busy || !status?.configured || !status?.enabled
    || !status?.autopilot_available || Boolean(status?.autopilot);
  element("autopilot-off").disabled = busy || !status?.autopilot;
  element("longshot-on").disabled = busy || !status?.configured || !status?.enabled
    || !status?.longshot_available || Boolean(status?.longshot);
  element("longshot-off").disabled = busy || !status?.longshot;
  element("save-caps").disabled = busy || !status?.configured;
  element("save-budget").disabled = busy || !status?.configured;
  element("start-trading").disabled = busy || stopping || !status?.configured || !status?.enabled
    || Boolean(status?.unknown_or_pending) || budgetEdited || !status?.limits?.capital_budget_cents;
  element("setup").hidden = Boolean(saved.passcode);
  element("forget").hidden = !saved.passcode;
}

function renderAmountMode() {
  const dollars = amountMode === "dollars";
  element("contracts-mode").className = `mode${dollars ? "" : " active"}`;
  element("dollars-mode").className = `mode${dollars ? " active" : ""}`;
  element("quantity-label").textContent = dollars ? "Spend up to (USD)" : "Contracts";
  if (dollars) {
    // the server's per-order cap is the real limit; no client-side clamp
    element("quantity").value = (Number(element("quantity").value) || 1).toFixed(2);
    element("amount-hint").textContent = "Uses the worse current side price, then rounds down to whole contracts. The exact maximum debit and fee reserve appear before Buy.";
  } else {
    element("amount-hint").textContent = "Enter the number of contracts to review.";
  }
}

async function quantityForReview() {
  if (amountMode === "contracts") return exactQuantity(element("quantity").value);
  const spend = Number(element("quantity").value);
  if (!Number.isFinite(spend) || spend <= 0 || Math.round(spend * 100) !== spend * 100) {
    throw new Error("Enter a positive dollar amount with at most two decimal places.");
  }
  const live = await request("/api/data?kind=live");
  const call = (live.data?.calls ?? []).find((c) => c.ticker === element("ticker").value.trim());
  const worstPrice = Math.max(Number(call?.up_price), Number(call?.down_price));
  if (!Number.isFinite(worstPrice) || worstPrice <= 0 || worstPrice >= 1) {
    throw new Error("A two-sided live quote is needed to convert dollars to contracts.");
  }
  const contracts = Math.floor(spend / (worstPrice + 0.01));
  if (contracts < 1) throw new Error(`$${spend.toFixed(2)} is below one contract at the current quote.`);
  return exactQuantity(String(contracts));
}

const CAP_FIELDS = [
  ["cap-spend", "purchase_spend_cents", 100],
  ["cap-exposure", "open_exposure_cents", 100],
  ["cap-loss", "realized_loss_cents", 100],
  ["cap-order", "order_debit_cents", 100],
  ["cap-contracts", "max_contracts", 1],
];
let capsEdited = false;

let history = []; // recent autopilot checks, newest last (kept for this browser session)

function renderAutopilot(data) {
  const on = Boolean(data.autopilot);
  element("autopilot-state").textContent = !data.autopilot_available
    ? "Not available: the server has no Jev decision URL configured."
    : on && data.paused
      ? "ON but paused: press Resume trading to let it trade."
      : on
        ? "ON: Jev trades supported BTC 15-minute and hourly markets within your budget."
        : "Off: nothing is bought without your Buy click.";
  for (const note of data.autopilot_last || []) {
    if (!history.some((h) => h.at_ms === note.at_ms && h.ticker === note.ticker)) history.push(note);
  }
  history = history.slice(-30);
  const latest = history[history.length - 1];
  const every = data.autopilot_every_s || 1;
  element("every-s").textContent = String(every);
  if (latest && on) {
    const ago = Math.max(0, (Date.now() - latest.at_ms) / 1000);
    const left = latest.engine?.seconds_left;
    element("now-market").textContent = [
      latest.ticker,
      left == null ? null : `closes in ${clock(left - ago)}`,
      `checked ${Math.round(ago)} s ago (every ${every} s)`,
    ].filter(Boolean).join(" · ");
    element("now-outcome").textContent = plainOutcome(latest);
  } else {
    element("now-market").textContent = on ? `First check within ${every} s…` : "";
    element("now-outcome").textContent = "";
  }
  const lines = latest && on ? engineLines(latest.engine) : [];
  element("now-engine").hidden = !lines.length;
  element("now-engine").textContent = lines.join("\n");
  element("autopilot-log").textContent = history.slice().reverse().map((note) =>
    `${new Date(note.at_ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}  ${plainOutcome(note)}`,
  ).join("\n") || "No checks yet.";
  if (data.limits && !capsEdited) {
    for (const [id, key, scale] of CAP_FIELDS) {
      if (data.limits[key] !== undefined) element(id).value = String(data.limits[key] / scale);
    }
  }
}

function renderLongshot(data) {
  const on = Boolean(data.longshot);
  const resting = data.resting_orders || 0;
  element("longshot-state").textContent = !data.longshot_available
    ? "Not available on this server."
    : on && data.paused
      ? "ON but paused: press Resume trading to let it place orders."
      : on
        ? `ON: copying the paper long-shot seller with small resting bids (${resting} resting now).`
        : "Off: no resting orders.";
  const proof = data.longshot_paper_proof || {};
  element("longshot-proof").textContent = proof.reason
    ? `Paper record: ${proof.reason}${data.paper_proof?.required === false ? " (proof waived by you)" : ""}`
    : "";
  element("longshot-log").textContent = (data.longshot_last || []).slice().reverse().map((note) =>
    `${new Date(note.at_ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}  ${note.ticker}  ${note.outcome}`,
  ).join("\n") || "No orders yet.";
}

const usd = (value) => "$" + Number(value || 0).toFixed(2);

function meter(id, used, capCents) {
  const bar = element(`bar-${id}`);
  bar.max = capCents ? capCents / 100 : 1;
  bar.value = Math.min(Number(used || 0), bar.max);
}

function render(data, quiet = false) {
  status = data;
  // Freshness comes from the server: subtracting its account timestamp from this machine's
  // clock made a skewed local clock look like a dropped broker connection. Absent field
  // means an older server, so stay closed rather than enabling order buttons on a guess.
  ready = data.configured && data.enabled && !data.paused && !data.unknown_or_pending
    && data.account_fresh === true;
  element("mode").textContent = data.environment ? data.environment.toUpperCase() : "SETUP";
  element("account").hidden = !data.configured;
  element("state").textContent = data.paused ? data.auto_recovery ? "RECOVERING" : "STOPPED" : data.autopilot || data.longshot ? "RUNNING" : "MANUAL";
  element("state").className = `pill${data.paused ? " paused" : data.autopilot ? " on" : ""}`;
  element("now-updated").textContent = `updated ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}`;
  const limits = data.limits || {};
  element("cash").textContent = usd(data.available_balance);
  element("pnl").textContent = usd(data.realized_pnl);
  if (!budgetEdited) element("simple-budget").value = limits.capital_budget_cents ? (limits.capital_budget_cents / 100).toFixed(2) : "";
  if (!strategyEdited) element("strategy").value = data.autopilot && data.longshot ? "both" : data.longshot ? "longshot" : "autopilot";
  element("dashboard-link").href = validBase(saved.base) + "/#positions";
  const coverage = data.market_coverage;
  const scanner = data.scanner;
  element("strategy-help").textContent = `${scanner?.running ? "SCANNING LIVE" : "Scanner connecting"}${data.paused ? " · orders paused; market checks continue" : ""}. Scan target: Jev ${data.autopilot_every_s || 1}s · long-shot ${data.longshot_every_s || 1}s. BTC 15-minute + hourly strikes${coverage ? ` · ${coverage.entry_markets} entry markets · ${coverage.managed_positions} open positions managed` : ""}. No extra tabs needed. Slow broker/model replies can extend a scan.`;
  const of = (used, cents) => cents ? `${usd(used)} of ${usd(cents / 100)}` : usd(used);
  element("spent").textContent = of(data.purchase_spend, limits.purchase_spend_cents);
  element("budget").textContent = usd(data.remaining_purchase_budget);
  element("exposure").textContent = of(data.open_exposure, limits.open_exposure_cents);
  element("loss").textContent = of(data.realized_loss, limits.realized_loss_cents);
  meter("spend", data.purchase_spend, limits.purchase_spend_cents);
  meter("exposure", data.open_exposure, limits.open_exposure_cents);
  meter("loss", data.realized_loss, limits.realized_loss_cents);
  if (data.limits) {
    element("caps").textContent = `Each order: at most ${usd(limits.order_debit_cents / 100)} incl. fees${limits.max_contracts ? ` and ${limits.max_contracts} contract${limits.max_contracts === 1 ? "" : "s"}` : ""}.${data.realized_pnl !== undefined ? ` Profit/loss so far: ${Number(data.realized_pnl) >= 0 ? "+" : "−"}${usd(Math.abs(Number(data.realized_pnl)))}.` : ""}`;
  }
  element("scope").textContent = data.scope || "";
  renderAutopilot(data);
  renderLongshot(data);
  if (data.paused) element("autopilot-state").textContent = data.auto_recovery
    ? "Scanning · recovering order access"
    : data.scanner?.running ? "Scanning · orders paused" : "Trading stopped";
  else if (data.longshot && !data.autopilot) element("autopilot-state").textContent = "Long-shot bids are running";
  else if (data.autopilot) element("autopilot-state").textContent = "Jev is watching your markets";
  else element("autopilot-state").textContent = "Ready when you are";
  if (data.error) element("now-outcome").textContent = data.auto_recovery
    ? `Retrying every second · ${data.recovery_checks || 0}/3 clean account checks. Trading resumes automatically when healthy.`
    : plainOutcome({ outcome: `refused: ${data.error}` });
  element("positions").textContent = (data.positions || []).map((position) =>
    `${position.ticker} · ${position.side.toUpperCase()} · ${(position.units / 100).toFixed(2)} contracts · cost $${(position.cost / 100000000).toFixed(4)}`
  ).join("\n") || "No routed positions.";
  element("orders").textContent = (data.orders || []).slice(-10).reverse().map(formatOrder).join("\n\n") || "No routed orders.";
  chrome.storage.session.set({ history }).catch(() => {});
  if (quiet) return controls();
  element("message").textContent = data.reason || data.error || (data.unknown_or_pending
    ? "An order is unresolved. Budget stays reserved; reconcile before continuing."
    : "Connected.");
  controls();
}

function budgetCents() {
  const raw = element("simple-budget").value;
  if (!/^\d+(?:\.\d{1,2})?$/.test(raw)) throw new Error("Enter a budget in dollars, with up to two decimal places.");
  const cents = Math.round(Number(raw) * 100);
  if (cents < 1 || cents > 100000000) throw new Error("Enter a budget between $0.01 and $1,000,000.");
  return cents;
}
element("simple-budget").addEventListener("input", () => { budgetEdited = true; controls(); });
element("strategy").addEventListener("change", () => { strategyEdited = true; });
element("save-budget").addEventListener("click", () => run(async () => {
  const cents = budgetCents();
  await request("/api/execution?action=budget", { confirmed: true, budget_cents: cents });
  budgetEdited = false;
  render(await request("/api/execution?action=status"));
  element("message").textContent = `Budget saved: ${usd(cents / 100)}. Press Start to authorize automatic trading.`;
}));
element("start-trading").addEventListener("click", () => run(async () => {
  if (budgetEdited) throw new Error("Save your new budget before starting.");
  if (!status?.limits?.capital_budget_cents) throw new Error("Save a trading budget first.");
  const approach = element("strategy").value;
  if (!["autopilot", "longshot", "both"].includes(approach)) throw new Error("Choose a trading approach.");
  await request("/api/execution?action=start", {
    confirmed: true, strategy: approach, generation: status.generation,
  });
  strategyEdited = false;
  await clearProposal();
  render(await request("/api/execution?action=status"));
  element("message").textContent = status.paused
    ? "Trading is stopped. Check the status before starting again."
    : "Automatic trading started within your saved budget.";
}));

async function clearProposal() {
  proposal = null;
  idempotencyKey = null;
  element("proposal").hidden = true;
  await chrome.storage.session.remove("pending");
}

async function run(operation) {
  if (busy) return;
  busy = true;
  controls();
  try { await operation(); }
  catch (error) { element("message").textContent = error.message; }
  finally { busy = false; controls(); }
}

element("connect").addEventListener("click", () => run(async () => {
  const base = validBase(element("base").value);
  const typed = element("passcode").value;
  if (typed) saved.passcode = typed;
  saved.base = base;
  const permitted = await chrome.permissions.request({ origins: [base + "/*"] });
  if (!permitted) throw new Error("Dashboard access permission was not granted.");
  render(await request("/api/execution?action=status"));
  await fillTicker();
  // remembered only after the password worked
  await chrome.storage.local.set({ base: saved.base, passcode: saved.passcode });
  element("passcode").value = "";
}));

element("forget").addEventListener("click", () => run(async () => {
  saved.passcode = "";
  await chrome.storage.local.remove("passcode");
  await clearProposal();
  ready = false;
  element("account").hidden = true;
  element("message").textContent = "Password forgotten on this machine.";
}));

element("review").addEventListener("click", () => run(async () => {
  await clearProposal();
  element("message").textContent = "Checking prices, account and Jev…";
  const data = await request("/api/jev-proposal", {
    ticker: element("ticker").value.trim(),
    quantity: await quantityForReview(),
  });
  if (data.status !== "confirmation_required") {
    element("message").textContent = `Jev chose ${data.status}. No order.`;
    return;
  }
  proposal = data;
  idempotencyKey = crypto.randomUUID();
  await chrome.storage.session.set({ pending: { proposal, idempotencyKey, base: saved.base } });
  showProposal();
  element("message").textContent = "Proposal ready. Click Buy to send it, or ignore it to let it expire.";
}));

element("contracts-mode").addEventListener("click", () => {
  amountMode = "contracts";
  renderAmountMode();
  if (proposal) clearProposal();
});
element("dollars-mode").addEventListener("click", () => {
  amountMode = "dollars";
  renderAmountMode();
  if (proposal) clearProposal();
});

function showProposal() {
  element("proposal").hidden = false;
  element("proposal-details").textContent = proposalText(proposal);
  const intent = proposal.intent;
  element("confirm").textContent = `${intent.action === "sell" ? "Sell" : "Buy"} ${intent.quantity} ${intent.side.toUpperCase()} at $${intent.limit_price} (IOC)`;
}

element("confirm").addEventListener("click", () => run(async () => {
  if (!proposal || Date.now() >= proposal.expires_ms) {
    throw new Error("This proposal expired. Ask Jev again.");
  }
  const data = await request("/api/execution?action=confirm", {
    proposal_id: proposal.proposal_id, confirmation_token: proposal.confirmation_token,
    idempotency_key: idempotencyKey, intent: proposal.intent, confirmed: true,
  });
  element("message").textContent = formatOrder(data);
  element("orders").textContent = formatOrder(data);
  await clearProposal();
  ready = false;
}));

for (const [id] of CAP_FIELDS) element(id).addEventListener("input", () => { capsEdited = true; });

element("save-caps").addEventListener("click", () => run(async () => {
  const limits = {};
  if (status?.limits?.capital_budget_cents) limits.capital_budget_cents = status.limits.capital_budget_cents;
  for (const [id, key, scale] of CAP_FIELDS) {
    const value = Number(element(id).value);
    if (!Number.isFinite(value) || value <= 0 || Math.round(value) !== value) {
      throw new Error("Caps must be positive whole numbers.");
    }
    limits[key] = value * scale;
  }
  await request("/api/execution?action=limits", { confirmed: true, limits });
  capsEdited = false;
  render(await request("/api/execution?action=status"));
  element("message").textContent = "Caps saved.";
}));

for (const [id, enabled] of [["autopilot-on", true], ["autopilot-off", false]]) {
  element(id).addEventListener("click", () => run(async () => {
    await request("/api/execution?action=autopilot", { confirmed: true, enabled });
    render(await request("/api/execution?action=status"));
    element("message").textContent = enabled ? "Autopilot is ON." : "Autopilot is OFF.";
  }));
}

for (const [id, enabled] of [["longshot-on", true], ["longshot-off", false]]) {
  element(id).addEventListener("click", () => run(async () => {
    await request("/api/execution?action=longshot", { confirmed: true, enabled });
    render(await request("/api/execution?action=status"));
    element("message").textContent = enabled
      ? "Long-shot seller is ON."
      : "Long-shot seller is OFF; its resting orders were canceled.";
  }));
}

// Stop bypasses the request queue, including a slow Start/status response.
element("pause").addEventListener("click", async () => {
  if (stopping) return;
  stopping = true;
  controls();
  element("message").textContent = "Stopping new orders and canceling resting bids…";
  try {
    const result = await request("/api/execution?action=pause", { confirmed: true });
    if (status) status = { ...status, ...result };
    await clearProposal();
    if (status) render(status);
    element("message").textContent = "Stopped. Filled positions remain open.";
  } catch (error) {
    element("message").textContent = `Stop could not be confirmed: ${error.message}. Check Kalshi before continuing.`;
  } finally { stopping = false; controls(); }
});

for (const action of ["reconcile", "resume"]) {
  element(action).addEventListener("click", () => run(async () => {
    await request("/api/execution?action=" + action, { confirmed: true });
    if (action !== "reconcile") await clearProposal();
    render(await request("/api/execution?action=status"));
  }));
}
// The ticker box follows the current BTC 15-minute market (it rolls every 15 minutes)
// unless you type one; clearing the box brings the auto-fill back.
async function fillTicker() {
  if (!saved.passcode) return;
  const live = await request("/api/data?kind=live");
  const fifteen = (live.data?.calls ?? []).find((c) => c.series === "KXBTC15M");
  if (!fifteen) {
    element("ticker-hint").textContent = "No BTC 15-minute market is open right now.";
    return;
  }
  const close = new Date(live.data.updated_ms + fifteen.seconds_left * 1000);
  element("ticker-hint").textContent = `Current BTC 15-minute market, closes ${close.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  if (!tickerTyped || !element("ticker").value.trim()) {
    tickerTyped = false;
    if (element("ticker").value !== fifteen.ticker && proposal) await clearProposal();
    element("ticker").value = fifteen.ticker;
  }
}

for (const id of ["ticker", "quantity", "base"]) element(id).addEventListener("input", () => {
  if (id === "ticker") tickerTyped = Boolean(element("ticker").value.trim());
  if (id === "base") ready = false;
  if (proposal) clearProposal();
  controls();
});
setInterval(controls, 500);
setInterval(() => { fillTicker().catch(() => {}); }, 30000);
// keep the "Right now" card current while the popup is open
// (a failed refresh says so instead of silently leaving old checks on screen)
let refreshing = false;
async function refresh() {
  if (!saved.passcode || busy || refreshing) return;
  refreshing = true;
  try {
    render(await request("/api/execution?action=status"), true);
  } catch (error) {
    element("now-updated").textContent = "NOT UPDATING";
    element("message").textContent = `Couldn't refresh from the server: ${error.message} (retrying every second)`;
  } finally {
    refreshing = false;
  }
}
setInterval(refresh, 1000);

const stored = await chrome.storage.local.get(["base", "passcode"]);
saved = { base: stored.base || element("base").value, passcode: stored.passcode || "" };
element("base").value = saved.base;
history = (await chrome.storage.session.get("history")).history || [];
const pending = (await chrome.storage.session.get("pending")).pending;
if (pending && pending.base === saved.base && pending.proposal.expires_ms > Date.now()) {
  proposal = pending.proposal;
  idempotencyKey = pending.idempotencyKey;
  showProposal();
}
controls();
renderAmountMode();
if (saved.passcode) {
  await run(async () => {
    render(await request("/api/execution?action=status"));
    await fillTicker();
  });
}
