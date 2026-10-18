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
    return res.status(502).json({ error: error.message });
  }
}
