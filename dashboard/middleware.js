// Protect every static page and API route before a browser can reach the
// server-side paper-control route. The passcode stays in a Vercel environment
// variable and is never bundled into the dashboard.
export default function middleware(request) {
  const passcode = process.env.DASHBOARD_ACCESS_PASSWORD;
  if (!passcode) {
    return new Response("Dashboard access is not configured.", { status: 503 });
  }
  const supplied = request.headers.get("authorization");
  const expected = `Basic ${btoa(`edgebook:${passcode}`)}`;
  if (supplied === expected) return;
  return new Response("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="edgebook dashboard"' },
  });
}

export const config = { matcher: "/:path*" };
