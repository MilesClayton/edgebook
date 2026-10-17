import assert from "node:assert/strict";
import test from "node:test";
import { authorized, sessionCookie, allowedOrigin } from "../lib/access.js";
import middleware from "../middleware.js";
import handler from "../api/execution.js";
import jevHandler from "../api/jev-proposal.js";

const password = "fixture-passcode";
const environment = { DASHBOARD_ACCESS_PASSWORD: password, EXTENSION_ORIGIN: "chrome-extension://fixture" };
const authorization = `Basic ${btoa("edgebook:" + password)}`;
function response() {
  return {
    statusCode: 200, headers: {}, body: null,
    setHeader(name, value) { this.headers[name] = value; },
    status(code) { this.statusCode = code; return this; },
    json(body) { this.body = body; return this; },
  };
}

test("signed session expires and does not expose the dashboard password", async () => {
  const cookie = await sessionCookie(password, 1000);
  assert.ok(!cookie.includes(btoa(password)));
  assert.ok(await authorized({ authorization }, environment, 1000));
  assert.ok(await authorized({ cookie }, environment, 2000));
  assert.ok(!await authorized({ cookie: cookie + "bad" }, environment, 2000));
  assert.ok(!await authorized({ cookie }, environment, 30000000));
  assert.ok(!await authorized({ authorization }, {}, 2000));
  assert.ok(!await authorized({ cookie: "edgebook_access=garbage.signature" }, environment, 2000));
  assert.equal(allowedOrigin("https://evil.test", "dashboard.test", environment), false);
  assert.equal(allowedOrigin("chrome-extension://fixture", "dashboard.test", environment), true);
});

test("middleware restricts extension CORS and authenticates all actual API requests", async () => {
  const saved = { ...process.env };
  Object.assign(process.env, environment);
  try {
    const request = (method, headers = {}) => new Request("https://dashboard.test/api/execution", { method, headers });
    assert.equal((await middleware(request("OPTIONS", { Origin: "https://evil.test" }))).status, 403);
    const preflight = await middleware(request("OPTIONS", { Origin: environment.EXTENSION_ORIGIN }));
    assert.equal(preflight.status, 204);
    assert.equal(preflight.headers.get("Access-Control-Allow-Origin"), environment.EXTENSION_ORIGIN);
    assert.equal((await middleware(request("POST"))).status, 401);
    const allowed = await middleware(request("POST", { Authorization: authorization, Origin: environment.EXTENSION_ORIGIN }));
    assert.equal(allowed.headers.get("x-middleware-next"), "1");
    const login = await middleware(new Request("https://dashboard.test/", { headers: { Authorization: authorization } }));
    assert.equal(login.status, 302);
    assert.match(login.headers.get("Set-Cookie"), /HttpOnly; Secure; SameSite=Strict/);
    delete process.env.DASHBOARD_ACCESS_PASSWORD;
    assert.equal((await middleware(request("GET"))).status, 503);
  } finally { process.env = saved; }
});

test("operator proxy pins its upstream and never forwards browser credentials", async () => {
  const saved = { ...process.env };
  const originalFetch = globalThis.fetch;
  Object.assign(process.env, environment, {
    FLY_DATA_URL: "https://fixture-backend.test",
    EXECUTION_OPERATOR_TOKEN: "operator-server-only",
    EXECUTION_PROPOSAL_TOKEN: "proposal-server-only",
  });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify({ paused: true, configured: true }), {
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    let res = response();
    await handler({ method: "GET", headers: {}, query: { action: "status" } }, res);
    assert.equal(res.statusCode, 401);
    assert.equal(calls.length, 0);
    res = response();
    await handler({ method: "GET", headers: { authorization, host: "dashboard.test" },
      query: { action: "status", url: "https://evil.test" } }, res);
    assert.equal(calls[0].url, "https://fixture-backend.test/execution/status");
    assert.equal(calls[0].options.headers.Authorization, "Bearer operator-server-only");
    assert.ok(!JSON.stringify(res.body).includes("server-only"));
    res = response();
    await handler({ method: "POST", headers: { authorization, host: "dashboard.test",
      origin: "https://evil.test", "content-type": "application/json" }, query: { action: "confirm" } }, res);
    assert.equal(res.statusCode, 403);
    res = response();
    await handler({ method: "POST", headers: { authorization, "content-type": "application/json" },
      query: { action: "propose" } }, res);
    assert.equal(res.statusCode, 404);
    globalThis.fetch = async () => { throw new TypeError("fixture timeout"); };
    res = response();
    await handler({ method: "POST", headers: { authorization, "content-type": "application/json" },
      query: { action: "confirm" }, body: {} }, res);
    assert.equal(res.statusCode, 503);
    assert.equal(res.body.outcome, "check_backend_status_before_retrying");
    delete process.env.EXECUTION_OPERATOR_TOKEN;
    res = response();
    await handler({ method: "GET", headers: { authorization }, query: { action: "status" } }, res);
    assert.equal(res.statusCode, 503);
  } finally { globalThis.fetch = originalFetch; process.env = saved; }
});

test("browser cannot inject a decision, price, limits or account state into model review", async () => {
  const saved = { ...process.env };
  Object.assign(process.env, environment);
  try {
    for (const field of ["decision", "limit_price", "account_fresh", "limits"]) {
      const res = response();
      await jevHandler({
        method: "POST", headers: { authorization, "content-type": "application/json" },
        body: { ticker: "KXBTC15M-X", quantity: "1.00", [field]: true },
      }, res);
      assert.equal(res.statusCode, 400);
    }
  } finally { process.env = saved; }
});
