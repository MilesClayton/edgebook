import assert from "node:assert/strict";
import test from "node:test";
import handler from "../api/jev-decide.js";

function response() {
  return { statusCode: 0, body: null, headers: {},
    setHeader(name, value) { this.headers[name] = value; },
    status(code) { this.statusCode = code; return this; },
    json(body) { this.body = body; return this; } };
}

test("jev-decide requires the proposal token and a review context", async () => {
  process.env.EXECUTION_PROPOSAL_TOKEN = "fixture-token-value";
  const context = { allowed_actions: ["hold"] };
  const decide = async (c) => ({ action: "hold", confidence: 0.5, seen: c.allowed_actions });
  for (const authorization of [undefined, "Bearer wrong-token-value", "Bearer fixture-token-valu"]) {
    const res = response();
    await handler({ method: "POST", headers: { authorization }, body: { context } }, res, decide);
    assert.equal(res.statusCode, 401);
  }
  const auth = { authorization: "Bearer fixture-token-value" };
  let res = response();
  await handler({ method: "GET", headers: auth }, res, decide);
  assert.equal(res.statusCode, 405);
  res = response();
  await handler({ method: "POST", headers: auth, body: {} }, res, decide);
  assert.equal(res.statusCode, 400);
  res = response();
  await handler({ method: "POST", headers: auth, body: { context } }, res, decide);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.body.seen, ["hold"]);
  assert.equal(res.headers["Cache-Control"], "no-store");
  res = response();
  await handler({ method: "POST", headers: auth, body: { context } }, res,
    async () => { throw new Error("gateway down"); });
  assert.equal(res.statusCode, 502);
  delete process.env.EXECUTION_PROPOSAL_TOKEN;
  res = response();
  await handler({ method: "POST", headers: { authorization: "Bearer " }, body: { context } }, res, decide);
  assert.equal(res.statusCode, 401);
});
