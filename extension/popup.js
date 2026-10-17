import { formatOrder, proposalText, validBase, exactQuantity } from "./view.js";

const element = (id) => document.getElementById(id);
let proposal = null;
let idempotencyKey = null;
let ready = false;
let busy = false;
let status = null;

async function request(path, body) {
  const base = validBase(element("base").value);
  const passcode = element("passcode").value;
  if (!passcode) throw new Error("Enter the dashboard passcode.");
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
  return data;
}

function controls() {
  for (const id of ["connect", "pause", "reconcile", "resume"]) element(id).disabled = busy;
  element("review").disabled = busy || !ready;
  element("confirm").disabled = busy || !ready || !proposal
    || !element("confirm-check").checked || Date.now() >= proposal.expires_ms;
  element("resume").disabled = busy || !status?.configured || !status?.enabled
    || !element("resume-check").checked;
}

function render(data) {
  status = data;
  ready = data.configured && data.enabled && !data.paused && !data.unknown_or_pending
    && Date.now() - data.account_updated_ms <= 5000;
  element("mode").textContent = data.environment ? data.environment.toUpperCase() : "SETUP";
  element("account").hidden = !data.configured;
  element("state").textContent = data.paused ? "Paused" : "User-confirmed trading ready";
  element("budget").textContent = "$" + Number(data.remaining_purchase_budget || 0).toFixed(2);
  element("exposure").textContent = "$" + Number(data.open_exposure || 0).toFixed(2);
  element("loss").textContent = "$" + Number(data.realized_loss || 0).toFixed(2);
  if (data.limits) {
    const limits = data.limits;
    element("caps").textContent = `Caps: purchases $${(limits.purchase_spend_cents / 100).toFixed(2)}, exposure $${(limits.open_exposure_cents / 100).toFixed(2)}, loss $${(limits.realized_loss_cents / 100).toFixed(2)}, per order $${(limits.order_debit_cents / 100).toFixed(2)}. Fees included; purchases do not reset after exit.`;
  }
  element("scope").textContent = data.scope || "";
  element("positions").textContent = (data.positions || []).map((position) =>
    `${position.ticker} · ${position.side.toUpperCase()} · ${(position.units / 100).toFixed(2)} contracts · cost $${(position.cost / 100000000).toFixed(4)}`
  ).join("\n") || "No routed positions.";
  element("orders").textContent = (data.orders || []).slice(-10).reverse().map(formatOrder).join("\n\n") || "No routed orders.";
  element("message").textContent = data.reason || data.error || (data.unknown_or_pending
    ? "An order is unresolved. Budget stays reserved; reconcile before continuing."
    : "Status refreshed. Every order requires explicit confirmation.");
  controls();
}

async function clearProposal() {
  proposal = null;
  idempotencyKey = null;
  element("proposal").hidden = true;
  element("confirm-check").checked = false;
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
  const permitted = await chrome.permissions.request({ origins: [base + "/*"] });
  if (!permitted) throw new Error("Dashboard access permission was not granted.");
  await chrome.storage.local.set({ base });
  render(await request("/api/execution?action=status"));
}));

element("review").addEventListener("click", () => run(async () => {
  await clearProposal();
  element("message").textContent = "Checking prices, account and bounded Jev decision…";
  const data = await request("/api/jev-proposal", {
    ticker: element("ticker").value.trim(),
    quantity: exactQuantity(element("quantity").value),
  });
  if (data.status !== "confirmation_required") {
    element("message").textContent = `Jev chose ${data.status}. No order submitted.`;
    return;
  }
  proposal = data;
  idempotencyKey = crypto.randomUUID();
  await chrome.storage.session.set({ pending: { proposal, idempotencyKey, base: validBase(element("base").value) } });
  element("proposal").hidden = false;
  element("proposal-details").textContent = proposalText(data);
  element("message").textContent = "Proposal only. Review the exact terms before confirming.";
}));

element("confirm").addEventListener("click", () => run(async () => {
  if (!proposal || !element("confirm-check").checked || Date.now() >= proposal.expires_ms) {
    throw new Error("A fresh proposal and explicit confirmation are required.");
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

for (const action of ["pause", "reconcile", "resume"]) {
  element(action).addEventListener("click", () => run(async () => {
    if (action === "resume" && !element("resume-check").checked) return;
    await request("/api/execution?action=" + action, { confirmed: true });
    if (action !== "reconcile") await clearProposal();
    render(await request("/api/execution?action=status"));
  }));
}
for (const id of ["confirm-check", "resume-check"]) element(id).addEventListener("change", controls);
for (const id of ["ticker", "quantity", "base"]) element(id).addEventListener("input", () => {
  if (id === "base") ready = false;
  if (proposal) clearProposal();
  controls();
});
setInterval(controls, 500);
const saved = await chrome.storage.local.get("base");
if (saved.base) element("base").value = saved.base;
const pending = (await chrome.storage.session.get("pending")).pending;
if (pending && pending.base === validBase(element("base").value) && pending.proposal.expires_ms > Date.now()) {
  proposal = pending.proposal;
  idempotencyKey = pending.idempotencyKey;
  element("proposal").hidden = false;
  element("proposal-details").textContent = proposalText(proposal);
}
controls();
