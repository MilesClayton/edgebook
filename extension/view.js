export function validBase(value) {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.username || url.password || url.pathname !== "/"
      || url.search || url.hash) throw new Error("Use an HTTPS dashboard origin without a path.");
  return url.origin;
}

export function exactQuantity(value) {
  if (!/^(?:0|[1-9][0-9]{0,4})(?:\.[0-9]{1,2})?$/.test(value) || Number(value) <= 0) {
    throw new Error("Enter a positive contract quantity with at most two decimal places.");
  }
  return Number(value).toFixed(2);
}

export function proposalText(proposal) {
  const intent = proposal.intent;
  return [
    `${intent.action.toUpperCase()} ${intent.side.toUpperCase()} · ${intent.ticker}`,
    `${intent.quantity} contracts at limit $${intent.limit_price}`,
    intent.action === "sell"
      ? `Minimum proceeds after reserved fees: $${Number(proposal.minimum_exit_proceeds).toFixed(4)}`
      : `Maximum debit including reserved fees: $${Number(proposal.maximum_debit).toFixed(4)}`,
    `Reserved fee bound: $${Number(proposal.fee_reserve).toFixed(4)}`,
    `Engine settlement estimate: ${(proposal.estimated_settlement_probability * 100).toFixed(1)}%`,
    intent.action === "sell"
      ? `Exit advantage after reserved fees: ${(proposal.exit_advantage_after_reserved_fees * 100).toFixed(2)}¢/contract`
      : `Estimated edge after reserved fees: ${(proposal.estimated_edge_after_reserved_fees * 100).toFixed(2)}¢/contract`,
    `Jev decision confidence: ${(proposal.decision.confidence * 100).toFixed(1)}% (not settlement probability)`,
    `Expires: ${new Date(proposal.expires_ms).toLocaleTimeString()}`,
    "IOC: may fill partially; any remainder is canceled.",
  ].join("\n");
}

export function formatOrder(order) {
  if (order.status === "unknown" || order.status === "pending") {
    return `${order.intent?.ticker || ""} · Outcome unresolved. It may have reached Kalshi. Reservation retained; reconcile before another order.`;
  }
  if (order.status === "rejected") return order.submission_attempted === false
    ? `${order.intent?.ticker || ""} · Stopped before broker dispatch. No order sent.`
    : `${order.intent?.ticker || ""} · Broker rejected the order; no fill recorded.`;
  return `${order.intent?.ticker || ""} · ${order.broker_acknowledged ? "Broker acknowledged" : "Not acknowledged"} · ${order.status}\nFilled ${order.filled_quantity} contracts · cost $${order.fill_cost} · fees $${order.fees}\nBroker order: ${order.order_id || "unavailable"}`;
}

const cents = (x) => `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}¢`;
const pct = (x) => `${Math.round(x * 100)}%`;

/** One autopilot check in plain words: what happened and why. */
export function plainOutcome(note) {
  const outcome = String(note.outcome || "");
  const jev = note.jev ? `Jev said ${note.jev.action.replace("_", " ")} (${pct(note.jev.confidence)} sure)` : "";
  if (outcome.startsWith("refused: deterministic data-quality gates failed")) {
    const failed = note.engine?.failed_gates?.length ? note.engine.failed_gates
      : outcome.split(": ").slice(2).join(": ").split(", ").filter(Boolean);
    return `Waiting for good data: ${failed.join(", ") || "a data check failed"}. No order.`;
  }
  if (outcome.startsWith("no trade")) return "No trade: neither side is cheap enough to beat fees. Jev wasn't asked.";
  if (outcome.startsWith("Jev chose")) return `${jev}. No order.`;
  if (outcome.startsWith("already holding")) return "Already holding this market. Autopilot never buys more of it.";
  if (outcome.startsWith("order ")) {
    const o = note.order || {};
    const what = o.intent ? `${o.intent.action === "sell" ? "Sold" : "Bought"} ${o.intent.side === "yes" ? "UP" : "DOWN"}` : "Order";
    return `${jev}. ${what}: filled ${o.filled_quantity ?? "0"} for $${o.fill_cost ?? "0"} + $${o.fees ?? "0"} fees (${o.status}).`;
  }
  if (outcome.startsWith("error: ")) return `Couldn't get a usable answer from Jev (${outcome.slice(7)}). No order.`;
  if (outcome.startsWith("refused: ")) return `Blocked by a safety check: ${outcome.slice(9)}. No order.`;
  return outcome;
}

/** What the engine saw for each side: price, its estimate, and the edge vs what's needed. */
export function engineLines(engine) {
  if (!engine || engine.fair_up == null) return [];
  const side = (name, price, probability, edge) => price == null ? `${name}: no price`
    : `${name} costs ${Math.round(price * 100)}¢ · engine says ${pct(probability)} · edge ${edge == null ? "–" : cents(edge)}`;
  const lines = [
    side("UP", engine.up_price, engine.fair_up, engine.edge_up),
    side("DOWN", engine.down_price, 1 - engine.fair_up, engine.edge_down),
    `A trade needs an edge of at least ${cents(engine.edge_needed ?? 0)} per contract after fees.`,
  ];
  const flow = [];
  if (engine.share_bought_up_5m != null) flow.push(`last 5 min ${pct(engine.share_bought_up_5m)} of contracts bought UP`);
  if (engine.index_change_5m_bps != null) {
    flow.push(`BTC ${engine.index_change_5m_bps >= 0 ? "+" : ""}${(engine.index_change_5m_bps / 100).toFixed(2)}% in 5 min`);
  }
  if (flow.length) lines.push(`Market: ${flow.join(" · ")}`);
  return lines;
}

/** Minutes:seconds, e.g. 7:05. */
export function clock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
