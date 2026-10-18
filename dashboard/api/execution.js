import { requireAccess } from "../lib/access.js";
import { backend } from "../lib/execution-backend.js";

export default async function handler(req, res) {
  if (!await requireAccess(req, res)) return;
  const action = req.query?.action;
  if (!["status", "confirm", "pause", "resume", "reconcile", "limits", "autopilot", "longshot"].includes(action)) {
    return res.status(404).json({ error: "unknown execution action" });
  }
  if (req.method !== (action === "status" ? "GET" : "POST")) {
    return res.status(405).json({ error: "method not allowed" });
  }
  try {
    return res.status(200).json(await backend(action, action === "status" ? undefined : req.body));
  } catch (error) {
    return res.status(error.status || 503).json({
      error: error.message,
      ...(action === "confirm" ? { outcome: "check_backend_status_before_retrying" } : {}),
    });
  }
}
