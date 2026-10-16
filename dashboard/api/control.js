import crypto from "node:crypto";

const STORE = "https://edgebook-recorder.fly.dev";
const ACTIONS = new Set(["start", "stop"]);

export default async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ error: "POST required" });
  const storeToken = process.env.DASHBOARD_TOKEN;
  if (!storeToken) return res.status(503).json({ error: "control plane is not configured" });
  const action = req.body?.action;
  if (!ACTIONS.has(action)) return res.status(400).json({ error: "invalid action" });
  const data = {
    id: crypto.randomUUID(),
    requested_at: new Date().toISOString(),
    action,
    config: action === "start" ? req.body.config : undefined,
  };
  try {
    const base = (process.env.FLY_DATA_URL || STORE).replace(/\/$/, "");
    const response = await fetch(`${base}/dashboard/control`, {
      method: "POST",
      headers: { Authorization: `Bearer ${storeToken}`, "Content-Type": "application/json" },
      body: JSON.stringify({ data }),
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) return res.status(502).json({ error: `control store returned ${response.status}` });
    return res.status(202).json({ ok: true, id: data.id });
  } catch (error) {
    return res.status(504).json({ error: `control store unreachable: ${error.name}` });
  }
}
