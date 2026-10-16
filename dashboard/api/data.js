// GET /api/data?kind=snapshot|research|calibration|trades|execution
// Proxies the JSON store served by the Fly recorder. Market/model kinds are
// public there; the trade log needs DASHBOARD_TOKEN (set it in Vercel env to
// show trades). The token, if set, stays server-side.
const KINDS = new Set(["snapshot", "research", "calibration", "trades", "execution"]);
const DEFAULT_STORE = "https://edgebook-recorder.fly.dev";

export default async function handler(req, res) {
  const kind = String(req.query.kind ?? "");
  if (!KINDS.has(kind)) return res.status(400).json({ error: "unknown kind" });
  const base = (process.env.FLY_DATA_URL || DEFAULT_STORE).replace(/\/$/, "");
  const token = process.env.DASHBOARD_TOKEN;
  try {
    const r = await fetch(`${base}/dashboard/${kind}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: AbortSignal.timeout(8000),
    });
    if (r.status === 404) return res.status(404).json({ error: `no ${kind} published yet` });
    if (r.status === 401) return res.status(404).json({ error: `${kind} requires DASHBOARD_TOKEN` });
    if (!r.ok) return res.status(502).json({ error: `store returned ${r.status}` });
    res.setHeader("Cache-Control", "private, max-age=15");
    res.setHeader("Content-Type", "application/json");
    return res.status(200).send(await r.text());
  } catch (e) {
    return res.status(504).json({ error: `store unreachable: ${e.name}` });
  }
}
