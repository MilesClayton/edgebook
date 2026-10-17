import { requireAccess } from "../lib/access.js";
import { backend } from "../lib/execution-backend.js";
import { jevDecision } from "../lib/jev.js";

export default async function handler(req, res) {
  if (!await requireAccess(req, res)) return;
  if (req.method !== "POST") return res.status(405).json({ error: "POST required" });
  const body = req.body;
  if (!body || Object.keys(body).some((key) => !["ticker", "quantity"].includes(key))) {
    return res.status(400).json({ error: "Only ticker and exact quantity are accepted" });
  }
  try {
    const review = await backend("review", body, "proposal");
    const decision = await jevDecision(review.context);
    const proposal = await backend("propose", { review_id: review.review_id, decision }, "proposal");
    return res.status(200).json(proposal);
  } catch (error) {
    return res.status(error.status || 503).json({ error: error.message, submitted: false });
  }
}
