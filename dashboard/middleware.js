import { allowedOrigin, authorized, sessionCookie } from "./lib/access.js";

export default async function middleware(request) {
  // server-to-server route with its own bearer-token check (api/jev-decide.js)
  if (new URL(request.url).pathname === "/api/jev-decide") return;
  const passcode = process.env.DASHBOARD_ACCESS_PASSWORD;
  const origin = request.headers.get("origin");
  const host = new URL(request.url).host;
  if (!allowedOrigin(origin, host)) return new Response("Origin not allowed", { status: 403 });
  const cors = origin && origin === process.env.EXTENSION_ORIGIN ? {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Vary": "Origin",
  } : {};
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: cors });
  }
  if (!passcode) return new Response("Dashboard access is not configured", { status: 503 });
  if (!await authorized(request.headers)) {
    return new Response("Authentication required", {
      status: 401, headers: { ...cors, "WWW-Authenticate": 'Basic realm="edgebook dashboard"' },
    });
  }
  if (new URL(request.url).pathname.startsWith("/api/")) {
    if (Object.keys(cors).length) {
      return new Response(null, {
        headers: { ...cors, "x-middleware-next": "1" },
      });
    }
    return;
  }
  if (request.headers.get("authorization") && !request.headers.get("cookie")) {
    return new Response(null, {
      status: 302,
      headers: {
        Location: request.url,
        "Set-Cookie": `${await sessionCookie(passcode)}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=28800`,
      },
    });
  }
}

export const config = { matcher: "/:path*" };
