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
