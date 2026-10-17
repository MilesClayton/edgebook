const encoder = new TextEncoder();

async function signature(password, value) {
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(password), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const result = new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(value)));
  return Array.from(result, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function equal(left, right) {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index++) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

export async function sessionCookie(password, now = Date.now()) {
  const expires = String(now + 8 * 60 * 60 * 1000);
  return `edgebook_access=${expires}.${await signature(password, expires)}`;
}

export async function authorized(headers, environment = process.env, now = Date.now()) {
  const password = environment.DASHBOARD_ACCESS_PASSWORD;
  if (!password) return false;
  const get = (name) => typeof headers.get === "function" ? headers.get(name) : headers[name];
  const supplied = get("authorization") || "";
  const expected = `Basic ${btoa(`edgebook:${password}`)}`;
  if (equal(supplied, expected)) return true;
  const cookie = (get("cookie") || "").split(";").map((item) => item.trim())
    .find((item) => item.startsWith("edgebook_access="));
  const [expires, mac] = (cookie || "").slice("edgebook_access=".length).split(".");
  if (!expires || !mac || !/^\d+$/.test(expires) || Number(expires) <= now
      || Number(expires) > now + 8 * 60 * 60 * 1000) return false;
  return equal(mac, await signature(password, expires));
}

export function allowedOrigin(origin, host, environment = process.env) {
  return !origin || origin === `https://${host}`
    || (Boolean(environment.EXTENSION_ORIGIN) && origin === environment.EXTENSION_ORIGIN);
}

export async function requireAccess(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (!allowedOrigin(req.headers.origin, req.headers.host)) {
    res.status(403).json({ error: "origin not allowed" });
    return false;
  }
  if (!await authorized(req.headers)) {
    res.status(401).json({ error: "dashboard authentication required" });
    return false;
  }
  if (req.method === "POST" && req.headers["content-type"]?.split(";")[0] !== "application/json") {
    res.status(415).json({ error: "application/json required" });
    return false;
  }
  return true;
}
