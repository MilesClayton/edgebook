from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from edgebook.execution.broker import BrokerRejectedError, KalshiBroker, fixed, timestamp
from edgebook.execution.live_ledger import CENT, DOLLAR, GuardError
from edgebook.venues.kalshi.auth import KalshiSigner
from tests.execution_helpers import NOW, TICKER, intent


def wire_order(**updates: Any) -> dict[str, Any]:
    return {
        "order_id": "fixture-order",
        "client_order_id": "fixture-client",
        "ticker": TICKER,
        "book_side": "bid",
        "initial_count_fp": "1.00",
        "yes_price_dollars": "0.4000",
        "subaccount_number": 2,
        "fill_count_fp": "0.37",
        "remaining_count_fp": "0.00",
        "status": "canceled",
        "taker_fees_dollars": "0.005",
        "maker_fees_dollars": "0",
        **updates,
    }


def wire_fill(**updates: Any) -> dict[str, Any]:
    return {
        "fill_id": "fixture-fill",
        "order_id": "fixture-order",
        "ticker": TICKER,
        "book_side": "bid",
        "subaccount_number": 2,
        "count_fp": "0.37",
        "yes_price_dollars": "0.4000",
        "fee_cost": "0.005",
        "created_time": datetime.fromtimestamp(NOW / 1000, UTC).isoformat(),
        "is_taker": True,
        **updates,
    }


def fixture_routes() -> dict[str, dict[str, Any]]:
    return {
        "/exchange/user_data_timestamp": {
            "as_of_time": datetime.fromtimestamp(NOW / 1000, UTC).isoformat(),
        },
        "/exchange/status": {"trading_active": True, "exchange_active": True},
        "/portfolio/balance": {"balance": 1000},
        "/portfolio/positions": {
            "market_positions": [{"ticker": TICKER, "position_fp": "0.37", "exchange_index": 2}],
        },
        "/portfolio/orders": {"orders": []},
        f"/markets/{TICKER}": {
            "market": {
                "ticker": TICKER,
                "event_ticker": "KXBTC15M-EVENT",
                "exchange_index": 2,
            }
        },
        "/events/KXBTC15M-EVENT": {
            "event": {
                "series_ticker": "KXBTC15M",
                "fee_type_override": None,
                "fee_multiplier_override": None,
            }
        },
        "/series/KXBTC15M": {"series": {"fee_type": "quadratic", "fee_multiplier": 1}},
        "/series/fee_changes": {"series_fee_change_arr": []},
        "/events/fee_changes": {"event_fee_changes": []},
        f"/markets/{TICKER}/orderbook": {
            "orderbook_fp": {
                "yes_dollars": [["0.3000", "1.00"], ["0.3900", "2.00"]],
                "no_dollars": [["0.6000", "3.00"]],
            }
        },
        "/portfolio/orders/fixture-order": {"order": wire_order()},
        "/portfolio/fills": {"fills": [wire_fill()]},
    }


def broker_for(
    signer: KalshiSigner, handler: Callable[[httpx.Request], httpx.Response]
) -> KalshiBroker:
    return KalshiBroker(
        signer,
        "demo",
        2,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )


def static_reply(body: Any, code: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json=body)

    return handle


def route_reply(
    routes: dict[str, dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=routes[request.url.path.removeprefix("/trade-api/v2")])

    return handle


async def test_signed_account_quote_and_paginated_orders(signer: KalshiSigner) -> None:
    routes = fixture_routes()
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["KALSHI-ACCESS-KEY"] == "kid"
        path = request.url.path.removeprefix("/trade-api/v2")
        return httpx.Response(200, json=routes[path])

    broker = broker_for(signer, handle)
    account = await broker.account()
    assert account["balance"] == 1000 * CENT
    assert account["positions"] == {TICKER: 37}
    quote = await broker.quote(TICKER, "KXBTC15M")
    assert quote["bids"]["yes"] == (39 * CENT, 200)
    assert quote["bids"]["no"] == (60 * CENT, 300)
    assert all(
        request.url.params.get("subaccount") == "2"
        for request in seen
        if "/portfolio/" in request.url.path
    )
    # BTC markets live on Kalshi's crypto shard; every account read is scoped to it
    assert all(
        request.url.params.get("exchange_index") == "2"
        for request in seen
        if request.url.path.endswith(("/balance", "/positions", "/orders"))
    )
    assert await broker.find_order("absent", None) is None
    routes["/portfolio/orders"]["orders"] = [wire_order()]
    assert (await broker.find_order("fixture-client", None)) is not None
    order = await broker.find_order("fixture-client", "fixture-order")
    assert order is not None
    fills = await broker.order_fills(order, intent())
    assert fills[0]["units"] == 37
    assert fills[0]["cost"] == 14_800_000
    await broker.close()


async def test_order_404_falls_back_to_scoped_list(signer: KalshiSigner) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["subaccount"] == "2"
        assert request.url.params["exchange_index"] == "2"
        if request.url.path.endswith("/fixture-order"):
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"orders": [wire_order()]})

    broker = broker_for(signer, handle)
    try:
        order = await broker.find_order("fixture-client", "fixture-order")
        assert order is not None
        assert order["order_id"] == "fixture-order"
    finally:
        await broker.close()


@pytest.mark.parametrize(
    ("action", "side", "book", "yes_price", "reduce"),
    [
        ("buy", "yes", "bid", "0.4000", False),
        ("buy", "no", "ask", "0.6000", False),
        ("sell", "yes", "ask", "0.4000", True),
        ("sell", "no", "bid", "0.6000", True),
    ],
)
async def test_order_direction_ioc_and_exact_price(
    signer: KalshiSigner, action: str, side: str, book: str, yes_price: str, reduce: bool
) -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "external-api.demo.kalshi.co"
        assert request.url.path == "/trade-api/v2/portfolio/events/orders"
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(201, json={"order_id": "fixture", "client_order_id": "client"})

    broker = broker_for(signer, handle)
    await broker.submit(intent(action=action, side=side), "client")
    body = seen[0]
    assert body["side"] == book
    assert body["price"] == yes_price
    assert body["reduce_only"] == reduce
    assert body["count"] == "1.00"
    assert body["time_in_force"] == "immediate_or_cancel"
    assert body["subaccount"] == 2
    assert body["exchange_index"] == 2
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    await broker.close()


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422, 429, 500, 503])
async def test_post_errors_are_never_retried(signer: KalshiSigner, code: int) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(code, json={})

    broker = broker_for(signer, handle)
    expected = BrokerRejectedError if code in {400, 401, 403, 404, 422} else httpx.HTTPStatusError
    with pytest.raises(expected):
        await broker.submit(intent(), "client")
    assert calls == 1
    await broker.close()


async def test_post_timeout_malformed_and_mismatched_receipt(signer: KalshiSigner) -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fixture")

    broker = broker_for(signer, timeout)
    with pytest.raises(httpx.ReadTimeout):
        await broker.submit(intent(), "client")
    await broker.close()
    responses: list[Any] = [{}, [], {"order_id": "fixture", "client_order_id": "other"}]
    for response in responses:
        broker = broker_for(signer, static_reply(response, 201))
        with pytest.raises(GuardError):
            await broker.submit(intent(), "client")
        await broker.close()


async def test_account_and_quote_fail_closed(signer: KalshiSigner) -> None:
    modifications: list[tuple[str, dict[str, Any]]] = [
        ("/exchange/status", {"trading_active": False, "exchange_active": True}),
        ("/portfolio/balance", {"balance": -1}),
        ("/portfolio/balance", {"balance": True}),
        # resting orders are returned, and the ledger refuses any it did not place
        # (test_execution_longshot.test_ledger_account_tolerates_only_our_resting_fills)
        (
            "/portfolio/positions",
            {
                "market_positions": [
                    {"ticker": TICKER, "position_fp": "1", "exchange_index": 1},
                ]
            },
        ),
    ]
    for path, replacement in modifications:
        routes = fixture_routes()
        routes[path] = replacement
        broker = broker_for(signer, route_reply(routes))
        with pytest.raises(GuardError):
            await broker.account()
        await broker.close()
    quote_modifications: list[tuple[str, dict[str, Any]]] = [
        ("/series/KXBTC15M", {"series": {"fee_type": "unknown", "fee_multiplier": 1}}),
        ("/series/KXBTC15M", {"series": {"fee_type": "quadratic", "fee_multiplier": 2}}),
        ("/events/KXBTC15M-EVENT", {"event": {"series_ticker": "OTHER"}}),
        (
            "/events/KXBTC15M-EVENT",
            {
                "event": {
                    "series_ticker": "KXBTC15M",
                    "fee_type_override": "quadratic",
                    "fee_multiplier_override": 2,
                }
            },
        ),
        (
            "/series/fee_changes",
            {
                "series_fee_change_arr": [
                    {
                        "scheduled_ts": datetime.fromtimestamp(NOW / 1000 + 5, UTC).isoformat(),
                    }
                ]
            },
        ),
        (
            f"/markets/{TICKER}/orderbook",
            {
                "orderbook_fp": {
                    "yes_dollars": [],
                    "no_dollars": [["0.60", "3"]],
                }
            },
        ),
        (
            f"/markets/{TICKER}/orderbook",
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.60", "3"]],
                    "no_dollars": [["0.60", "3"]],
                }
            },
        ),
        (f"/markets/{TICKER}", {"market": {"ticker": "OTHER"}}),
    ]
    for path, replacement in quote_modifications:
        routes = fixture_routes()
        routes[path] = replacement
        broker = broker_for(signer, route_reply(routes))
        with pytest.raises(GuardError):
            await broker.quote(TICKER, "KXBTC15M")
        await broker.close()


async def test_fill_validation_and_no_cost_side_guessing(signer: KalshiSigner) -> None:
    routes = fixture_routes()
    broker = broker_for(
        signer,
        lambda request: httpx.Response(
            200, json=routes[request.url.path.removeprefix("/trade-api/v2")]
        ),
    )
    for field, value in [
        ("ticker", "OTHER"),
        ("book_side", "ask"),
        ("initial_count_fp", "2"),
        ("yes_price_dollars", "0.5"),
        ("subaccount_number", 0),
    ]:
        with pytest.raises(GuardError, match="confirmed"):
            await broker.order_fills(wire_order(**{field: value}), intent())
    for field, value in [
        ("order_id", "other"),
        ("ticker", "OTHER"),
        ("subaccount_number", 0),
        ("book_side", "ask"),
        ("exchange_index", 1),
    ]:
        routes["/portfolio/fills"]["fills"] = [wire_fill(**{field: value})]
        with pytest.raises(GuardError, match="identity"):
            await broker.order_fills(wire_order(), intent())
    routes["/portfolio/fills"]["fills"] = [wire_fill(), wire_fill()]
    with pytest.raises(GuardError, match="duplicate"):
        await broker.order_fills(wire_order(), intent())
    routes["/portfolio/fills"]["fills"] = []
    with pytest.raises(GuardError, match="consistent"):
        await broker.order_fills(wire_order(), intent())
    routes["/portfolio/fills"]["fills"] = [wire_fill()]
    with pytest.raises(GuardError, match="remaining"):
        await broker.order_fills(wire_order(remaining_count_fp="1"), intent())
    routes["/portfolio/fills"]["fills"] = [wire_fill(book_side="ask", yes_price_dollars="0.6")]
    result = await broker.order_fills(
        wire_order(book_side="ask", yes_price_dollars="0.6"), intent(side="no")
    )
    assert result[0]["cost"] == 14_800_000
    await broker.close()


async def test_pagination_limits_validation_and_duplicate_client_id(signer: KalshiSigner) -> None:
    def page(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == "next":
            return httpx.Response(200, json={"orders": [wire_order()]})
        return httpx.Response(200, json={"orders": [], "cursor": "next"})

    broker = broker_for(signer, page)
    assert len(await broker.pages("/orders", "orders")) == 1
    await broker.close()
    broker = broker_for(
        signer, lambda _: httpx.Response(200, json={"orders": [], "cursor": "same"})
    )
    with pytest.raises(GuardError, match="advance"):
        await broker.pages("/orders", "orders")
    await broker.close()
    bodies: list[Any] = [[], {"orders": "invalid"}, {"orders": [wire_order(), wire_order()]}]
    for body in bodies:
        broker = broker_for(signer, static_reply(body))
        with pytest.raises(GuardError):
            await broker.find_order("fixture-client", None)
        await broker.close()
    with pytest.raises(GuardError, match="timezone"):
        timestamp("2026-10-17T12:00:00")
    with pytest.raises(GuardError, match="string"):
        fixed(1, DOLLAR)


async def test_expired_dispatch_deadline_never_calls_broker(signer: KalshiSigner) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"order_id": "fixture"})

    broker = broker_for(signer, handle)
    with pytest.raises(BrokerRejectedError, match="deadline") as caught:
        await broker.submit(intent(), "fixture", deadline_ms=NOW)
    assert caught.value.dispatched is False
    assert calls == []
    await broker.close()


async def test_markets_on_another_shard_are_refused(signer: KalshiSigner) -> None:
    routes = fixture_routes()
    routes[f"/markets/{TICKER}"]["market"]["exchange_index"] = 0
    broker = broker_for(signer, route_reply(routes))
    with pytest.raises(GuardError, match="shard 0; execution is configured for shard 2"):
        await broker.market(TICKER)
    routes[f"/markets/{TICKER}"]["market"]["ticker"] = "OTHER"
    with pytest.raises(GuardError, match="identity mismatch"):
        await broker.market(TICKER)
    await broker.close()
