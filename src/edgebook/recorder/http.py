"""Tiny authenticated JSON store for the dashboard, served by the recorder.

GET  /healthz                 -> 200 (no auth; Fly health check)
GET  /dashboard/{kind}        -> latest JSON (public for market/model kinds; trades need the token)
POST /dashboard/{kind}        -> replace JSON for kind (Bearer token)

Files live on the recorder's volume under ``<data_dir>/dashboard/``. The
Vercel dashboard proxies reads server-side, so the token never reaches a
browser. Writes are atomic (temp file + rename).
"""

from __future__ import annotations

import hmac
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

KINDS = frozenset({"snapshot", "research", "calibration", "trades", "execution", "control"})
# Market data and model output only; readable without the token. Writes, and the
# bot's own trade log, always require it.
PUBLIC_READ = frozenset({"snapshot", "research", "calibration"})
MAX_BYTES = 8 * 1024 * 1024


def write_kind(directory: Path, kind: str, data: Any) -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"received_at": datetime.now(UTC).isoformat(), "data": data})
    path = directory / f"{kind}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(body)
    os.replace(tmp, path)
    return path


def make_app(directory: Path, token: str) -> web.Application:
    expected = f"Bearer {token}".encode()

    def authorized(request: web.Request) -> bool:
        return hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected)

    def kind_of(request: web.Request) -> str:
        kind = request.match_info["kind"]
        if kind not in KINDS:
            raise web.HTTPNotFound(text="unknown kind")
        return kind

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def get_kind(request: web.Request) -> web.Response:
        kind = kind_of(request)
        if kind not in PUBLIC_READ and not authorized(request):
            raise web.HTTPUnauthorized()
        path = directory / f"{kind}.json"
        if not path.exists():
            raise web.HTTPNotFound(text="not published yet")
        return web.Response(body=path.read_bytes(), content_type="application/json")

    async def post_kind(request: web.Request) -> web.Response:
        if not authorized(request):
            raise web.HTTPUnauthorized()
        kind = kind_of(request)
        if (request.content_length or 0) > MAX_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_BYTES, actual_size=request.content_length or 0
            )
        payload = await request.json()
        write_kind(directory, kind, payload.get("data") if isinstance(payload, dict) else None)
        return web.json_response({"ok": True, "kind": kind})

    app = web.Application(client_max_size=MAX_BYTES)
    app.add_routes(
        [
            web.get("/healthz", healthz),
            web.get("/dashboard/{kind}", get_kind),
            web.post("/dashboard/{kind}", post_kind),
        ]
    )
    return app


async def serve(directory: Path, token: str, port: int = 8080) -> web.AppRunner:
    runner = web.AppRunner(make_app(directory, token))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()  # noqa: S104 - Fly routes to the VM
    return runner
