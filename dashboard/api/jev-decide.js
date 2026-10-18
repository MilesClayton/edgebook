// POST /api/jev-decide: server-to-server, for autopilot on the recorder.
// The recorder sends a review context; Jev (via AI Gateway) returns a bounded decision.
// Authenticated with the proposal token the recorder already holds (not the dashboard
// password), so the middleware lets this one route through to its own check.
import { jevDecision } from "../lib/jev.js";

function sameSecret(supplied, expected) {
  if (!expected || supplied.length !== expected.length) return false;
  let difference = 0;
  for (let i = 0; i < supplied.length; i++) difference |= supplied.charCodeAt(i) ^ expected.charCodeAt(i);
  return difference === 0;
}

export default async function handler(req, res, decide = jevDecision) {
  res.setHeader("Cache-Control", "no-store");
  const token = process.env.EXECUTION_PROPOSAL_TOKEN || "";
  if (!sameSecret(String(req.headers.authorization || ""), `Bearer ${token}`) || !token) {
    return res.status(401).json({ error: "authentication required" });
  }
  if (req.method !== "POST") return res.status(405).json({ error: "POST required" });
  const context = req.body?.context;
  if (!context || !Array.isArray(context.allowed_actions)) {
    return res.status(400).json({ error: "review context required" });
  }
  try {
    return res.status(200).json(await decide(context));
  } catch (error) {
    const status = error.statusCode ?? error.cause?.statusCode;
    const name = String(error.name || "");
    let code = "jev_unavailable", message = "Jev provider is unavailable; will retry next check";
    if (status === 401 || status === 403) { code = "jev_auth"; message = "Jev gateway authentication failed; check the gateway API key"; }
    else if (status === 402) { code = "jev_credits"; message = "Jev gateway credits are exhausted"; }
    else if (status === 429) { code = "jev_rate_limit"; message = "Jev is rate limited; will retry next check"; }
    else if (/Timeout|Abort/.test(name)) { code = "jev_timeout"; message = "Jev took too long; will retry next check"; }
    else if (/InvalidResponse|TypeValidation/.test(name)) { code = "jev_invalid_answer"; message = "Jev returned an incomplete decision; will retry next check"; }
    console.error("Jev decision failed", { code, upstream_status: status ?? null, error_type: name });
    return res.status(502).json({ error: message, code });
  }
}
