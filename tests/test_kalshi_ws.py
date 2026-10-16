"""KalshiWebSocket against a real local websockets server."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from edgebook.venues.kalshi.auth import KalshiSigner
from edgebook.venues.kalshi.models import OrderbookDelta, OrderbookSnapshot, Trade
from edgebook.venues.kalshi.ws import KalshiWebSocket, Subscription


def snapshot(sid: int, seq: int) -> str:
    return json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": sid,
            "seq": seq,
            "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.40", "10.00"]]},
        }
    )


def delta(sid: int, seq: int) -> str:
    return json.dumps(
        {
            "type": "orderbook_delta",
            "sid": sid,
            "seq": seq,
            "msg": {
                "market_ticker": "T",
                "price_dollars": "0.40",
                "delta_fp": "-1.00",
                "side": "yes",
            },
        }
    )


def trade(sid: int, seq: int) -> str:
    return json.dumps(
        {
            "type": "trade",
            "sid": sid,
            "seq": seq,
            "msg": {
                "trade_id": "x",
                "market_ticker": "T",
                "yes_price_dollars": "0.41",
                "count_fp": "3.00",
                "taker_side": "yes",
                "ts_ms": 1,
            },
        }
    )


Script = Callable[[ServerConnection, int], Any]


@asynccontextmanager
async def server(
    script: Script,
) -> AsyncIterator[tuple[str, list[dict[str, Any]], list[dict[str, str]]]]:
    commands: list[dict[str, Any]] = []
    handshakes: list[dict[str, str]] = []
    conn_no = 0

    async def handler(ws: ServerConnection) -> None:
        nonlocal conn_no
        conn_no += 1
        handshakes.append(dict(ws.request.headers) if ws.request else {})
        await script(ws, conn_no)

    async def recorded(ws: ServerConnection) -> None:
        orig_recv = ws.recv

        async def recv(*a: Any, **k: Any) -> Any:
            raw = await orig_recv(*a, **k)
            commands.append(json.loads(raw))
            return raw

        ws.recv = recv  # type: ignore[method-assign]
        await handler(ws)

    async with serve(recorded, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/trade-api/ws/v2", commands, handshakes


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    real_sleep = asyncio.sleep
    monkeypatch.setattr("edgebook.venues.kalshi.ws.asyncio.sleep", lambda s: real_sleep(0))


async def take(it: AsyncIterator[Any], n: int) -> list[Any]:
    out = []
    async for m in it:
        out.append(m)
        if len(out) == n:
            break
    return out


async def test_signed_handshake_subscribe_and_parse(signer: KalshiSigner) -> None:
    async def script(ws: ServerConnection, n: int) -> None:
        await ws.recv()  # subscribe
        await ws.send(
            json.dumps(
                {"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 7}}
            )
        )
        await ws.send("not json")
        await ws.send(json.dumps({"type": "ok", "id": 2, "sid": 7, "seq": 1}))
        await ws.send(snapshot(7, 2))
        await ws.send(delta(7, 3))
        await ws.send(trade(8, 1))
        await ws.wait_closed()

    async with server(script) as (url, commands, handshakes):
        client = KalshiWebSocket(url, signer)
        client.add_markets(["orderbook_delta", "trade"], ["T"])
        msgs = await asyncio.wait_for(take(client.messages(), 3), 5)
        await client.close()

    assert [type(m) for m in msgs] == [OrderbookSnapshot, OrderbookDelta, Trade]
    assert handshakes[0]["kalshi-access-key"] == "kid"
    assert commands[0]["params"] == {
        "channels": ["orderbook_delta", "trade"],
        "market_tickers": ["T"],
    }


async def test_book_gap_reconnects_and_resubscribes(signer: KalshiSigner) -> None:
    async def script(ws: ServerConnection, n: int) -> None:
        await ws.recv()
        if n == 1:
            await ws.send(snapshot(1, 1))
            await ws.send(delta(1, 3))  # seq 2 missing
        else:
            await ws.send(snapshot(1, 1))
        await ws.wait_closed()

    async with server(script) as (url, commands, _):
        client = KalshiWebSocket(url, signer)
        client.add(Subscription(channels=("orderbook_delta",), market_tickers=("T",)))
        msgs = await asyncio.wait_for(take(client.messages(), 2), 5)
        await client.close()

    assert [type(m) for m in msgs] == [OrderbookSnapshot, OrderbookSnapshot]
    assert client.gaps == 1
    assert client.reconnects == 1
    assert len(commands) == 2  # resubscribed on the new connection


async def test_non_book_gap_is_logged_not_fatal(signer: KalshiSigner) -> None:
    async def script(ws: ServerConnection, n: int) -> None:
        await ws.send(trade(4, 1))
        await ws.send(trade(4, 5))
        await ws.wait_closed()

    async with server(script) as (url, _, _):
        client = KalshiWebSocket(url, signer)
        msgs = await asyncio.wait_for(take(client.messages(), 2), 5)
        await client.close()
    assert len(msgs) == 2
    assert client.gaps == 1
    assert client.reconnects == 0


async def test_add_while_connected_sends_immediately(signer: KalshiSigner) -> None:
    got = asyncio.Event()

    async def script(ws: ServerConnection, n: int) -> None:
        await ws.send(trade(1, 1))
        await ws.recv()
        got.set()
        await ws.send(trade(1, 2))
        await ws.wait_closed()

    async with server(script) as (url, commands, _):
        client = KalshiWebSocket(url, signer)
        it = client.messages()
        await asyncio.wait_for(take(it, 1), 5)
        client.add(Subscription(channels=("cfbenchmarks_value",), index_ids=("BRTI",)))
        await asyncio.wait_for(take(it, 1), 5)
        await asyncio.wait_for(got.wait(), 5)
        await client.close()
    assert commands[0]["params"] == {"channels": ["cfbenchmarks_value"], "index_ids": ["BRTI"]}
