import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from edgebook.config import Settings
from edgebook.core.types import Side
from edgebook.publish import clean, publish
from edgebook.recorder.http import make_app, write_kind


@dataclass
class D:
    x: float


def test_clean_makes_strict_json() -> None:
    out = clean(
        {
            "a": float("nan"),
            "b": [Decimal("1.5"), np.float64(2.0)],
            "c": D(math.inf),
            1: datetime(2026, 1, 1, tzinfo=UTC),
            "s": Side.YES,
        }
    )
    json.dumps(out, allow_nan=False)
    assert out == {
        "a": None,
        "b": [1.5, 2.0],
        "c": {"x": None},
        "1": "2026-01-01T00:00:00+00:00",
        "s": "yes",
    }


async def test_store_auth_roundtrip(tmp_path: Path) -> None:
    async with TestClient(TestServer(make_app(tmp_path, "secret"))) as client:
        assert (await client.get("/healthz")).status == 200
        # public read, not yet written
        assert (await client.get("/dashboard/snapshot")).status == 404
        assert (await client.get("/dashboard/trades")).status == 401  # trade log needs the token
        assert (await client.get("/dashboard/execution")).status == 401
        auth = {"Authorization": "Bearer secret"}
        assert (await client.get("/dashboard/trades", headers=auth)).status == 404
        assert (await client.get("/dashboard/nope", headers=auth)).status == 404
        r = await client.post("/dashboard/research", json={"data": {"k": 1}}, headers=auth)
        assert r.status == 200
        body = await (await client.get("/dashboard/research", headers=auth)).json()
        assert body["data"] == {"k": 1}
        assert "received_at" in body
        bad = {"Authorization": "Bearer wrong"}
        assert (await client.post("/dashboard/research", json={}, headers=bad)).status == 401
    write_kind(tmp_path, "snapshot", {"spot": 1})
    assert json.loads((tmp_path / "snapshot.json").read_text())["data"] == {"spot": 1}
    with pytest.raises(ValueError, match="unknown kind"):
        write_kind(tmp_path, "evil", {})


async def test_publish_posts_or_skips() -> None:
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200 if "good" in str(req.url) else 500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    assert not await publish(Settings(), "trades", [])  # not configured
    good = Settings(dashboard_url="https://good.test/", dashboard_token=SecretStr("t"))
    assert await publish(good, "research", {"x": float("nan")}, client=client)
    assert seen[0].url.path == "/dashboard/research"
    assert seen[0].headers["Authorization"] == "Bearer t"
    assert json.loads(seen[0].content) == {"data": {"x": None}}
    bad = Settings(dashboard_url="https://bad.test", dashboard_token=SecretStr("t"))
    assert not await publish(bad, "research", {}, client=client)
    with pytest.raises(ValueError, match="unknown kind"):
        await publish(good, "evil", {}, client=client)
