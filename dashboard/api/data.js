// GET /api/data?kind=snapshot|research|calibration|trades
// Proxies the JSON store served by the Fly recorder. The store's Bearer token
// stays server-side in DASHBOARD_TOKEN; the browser never sees it.
const KINDS = new Set(["snapshot", "research", "calibration", "trades"]);

export default async function handler(req, res) {
  const kind = String(req.query.kind ?? "");
  if (!KINDS.has(kind)) return res.status(400).json({ error: "unknown kind" });
  const base = process.env.FLY_DATA_URL;
  const token = process.env.DASHBOARD_TOKEN;
  if (!base || !token) return res.status(500).json({ error: "dashboard not configured" });
  try {
    const r = await fetch(`${base.replace(/\/$/, "")}/dashboard/${kind}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(8000),
    });
    if (r.status === 404) return res.status(404).json({ error: `no ${kind} published yet` });
    if (!r.ok) return res.status(502).json({ error: `store returned ${r.status}` });
    res.setHeader("Cache-Control", "private, max-age=15");
    res.setHeader("Content-Type", "application/json");
    return res.status(200).send(await r.text());
  } catch (e) {
    return res.status(504).json({ error: `store unreachable: ${e.name}` });
  }
}
