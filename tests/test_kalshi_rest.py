from decimal import Decimal

import httpx
import pytest

from edgebook.venues.kalshi.auth import KalshiSigner
from edgebook.venues.kalshi.ratelimit import TokenBucket
from edgebook.venues.kalshi.rest import KalshiRest, underlying_for
from tests.conftest import market

BASE = "https://example.test/trade-api/v2"


def client(handler: httpx.MockTransport, signer: KalshiSigner | None = None) -> KalshiRest:
    return KalshiRest(
        BASE,
        signer=signer,
        client=httpx.AsyncClient(transport=handler),
        bucket=TokenBucket(1e9, capacity=1e9),
    )


async def test_markets_paginates_and_maps_to_core() -> None:
    seen: list[httpx.URL] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req.url)
        if "cursor" not in req.url.params:
            return httpx.Response(200, json={"markets": [market("A")], "cursor": "c1"})
        return httpx.Response(200, json={"markets": [market("B")], "cursor": ""})

    rest = client(httpx.MockTransport(handle))
    ms = await rest.open_markets("KXBTCD")
    await rest.aclose()
    assert [m.ticker for m in ms] == ["A", "B"]
    assert ms[0].underlying == "BTC"
    assert ms[0].strike == Decimal("90299.99")
    assert seen[0].params["status"] == "open"
    assert seen[1].params["cursor"] == "c1"


async def test_settled_query_omits_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        assert "status" not in req.url.params
        assert req.url.params["min_close_ts"] == "0"
        return httpx.Response(200, json={"markets": []})

    from datetime import UTC, datetime

    rest = client(httpx.MockTransport(handle))
    assert (
        await rest.markets("KXBTCD", status=None, min_close=datetime(1970, 1, 1, tzinfo=UTC)) == []
    )


async def test_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("edgebook.venues.kalshi.rest.asyncio.sleep", no_sleep)
    calls = iter([429, 503, 200])

    def handle(req: httpx.Request) -> httpx.Response:
        code = next(calls)
        return httpx.Response(code, json={"markets": []} if code == 200 else {})

    assert await client(httpx.MockTransport(handle)).markets("KXBTCD") == []


async def test_gives_up_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("edgebook.venues.kalshi.rest.asyncio.sleep", no_sleep)
    rest = client(httpx.MockTransport(lambda req: httpx.Response(429)))
    with pytest.raises(RuntimeError, match="failed after"):
        await rest.markets("KXBTCD")


async def test_balance_is_signed(signer: KalshiSigner) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        assert req.headers["KALSHI-ACCESS-KEY"] == "kid"
        return httpx.Response(200, json={"balance": 12345, "balance_dollars": "123.45"})

    assert (await client(httpx.MockTransport(handle), signer).balance()).balance == 12345
    with pytest.raises(RuntimeError, match="requires an API key"):
        await client(httpx.MockTransport(handle)).balance()


def test_unknown_series() -> None:
    assert underlying_for("KXETHD") == "ETH"
    with pytest.raises(KeyError, match="unknown series"):
        underlying_for("NOPE")
