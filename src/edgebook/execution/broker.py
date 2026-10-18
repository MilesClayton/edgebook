"""Kalshi V2 order adapter (IOC taker orders, plus post-only resting orders for the long-shot
pilot); GET failures are retryable, POSTs are never retried."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from edgebook.execution.live_ledger import (
    CENT,
    DOLLAR,
    GuardError,
    Intent,
    dollars,
    money,
    quantity,
)
from edgebook.venues.kalshi.auth import KalshiSigner
from edgebook.venues.kalshi.ratelimit import TokenBucket

URLS = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "production": "https://external-api.kalshi.com/trade-api/v2",
}


class BrokerRejectedError(GuardError):
    def __init__(self, message: str, dispatched: bool = True) -> None:
        super().__init__(message)
        self.dispatched = dispatched


def timestamp(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise GuardError("broker timestamp missing timezone")
    return int(parsed.timestamp() * 1000)


def fixed(value: Any, scale: int) -> int:
    if not isinstance(value, str):
        raise GuardError("broker fixed-point value must be a string")
    return money(value) if scale == DOLLAR else quantity(value)


class KalshiBroker:
    def __init__(
        self,
        signer: KalshiSigner,
        environment: str,
        subaccount: int,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        exchange_index: int = 2,
    ) -> None:
        self.base = URLS[environment]
        self.signer, self.subaccount, self.clock = signer, subaccount, clock
        # Kalshi runs several matching engines ("shards"); crypto markets (all BTC) are on 2.
        # Every order, position, fill and the balance are scoped to this one shard.
        self.exchange_index = exchange_index
        self.client = client or httpx.AsyncClient(timeout=5.0, follow_redirects=False)
        self.read_bucket = TokenBucket(200)
        self.write_bucket = TokenBucket(50)

    async def close(self) -> None:
        await self.client.aclose()

    async def get(self, path: str, params: dict[str, str | int] | None = None) -> dict[str, Any]:
        await self.read_bucket.acquire()
        url = self.base + path
        response = await self.client.get(
            url, params=params, headers=self.signer.headers("GET", url)
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise GuardError("invalid broker response")
        return body

    async def pages(
        self, path: str, field: str, params: dict[str, str | int] | None = None
    ) -> list[dict[str, Any]]:
        query = {"limit": 1000, **(params or {})}
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(1000):
            body = await self.get(path, query)
            page = body[field]
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise GuardError("invalid broker page")
            rows.extend(page)
            cursor = body.get("cursor")
            if not cursor:
                return rows
            if not isinstance(cursor, str) or cursor in seen:
                raise GuardError("broker pagination did not advance")
            seen.add(cursor)
            query["cursor"] = cursor
        raise GuardError("broker pagination exceeded limit")

    async def account(self) -> dict[str, Any]:
        started = self.clock()
        as_of = timestamp((await self.get("/exchange/user_data_timestamp"))["as_of_time"])
        status = await self.get("/exchange/status")
        if status["trading_active"] is not True or status["exchange_active"] is not True:
            raise GuardError("exchange is paused")
        params: dict[str, str | int] = {
            "subaccount": self.subaccount,
            "exchange_index": self.exchange_index,
        }
        balance = await self.get("/portfolio/balance", params)  # cash on this shard only
        rows = await self.pages("/portfolio/positions", "market_positions", params)
        # resting orders are returned, not refused: the ledger accepts only the ones it placed
        resting = await self.pages("/portfolio/orders", "orders", {**params, "status": "resting"})
        amount = balance["balance"]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise GuardError("invalid broker balance")
        positions: dict[str, int] = {}
        for row in rows:
            ticker = row["ticker"]
            if ticker in positions or row.get("exchange_index", self.exchange_index) != (
                self.exchange_index
            ):
                raise GuardError("duplicate position or position on another exchange shard")
            positions[ticker] = fixed(row["position_fp"], 100)
        return {
            "as_of_ms": min(as_of, started),
            "balance": amount * CENT,
            "positions": positions,
            "resting_client_ids": [str(row.get("client_order_id") or "") for row in resting],
        }

    async def market(self, ticker: str) -> dict[str, Any]:
        market = (await self.get(f"/markets/{ticker}"))["market"]
        if market["ticker"] != ticker:
            raise GuardError("market identity mismatch")
        shard = market.get("exchange_index", 0)
        if shard != self.exchange_index:
            raise GuardError(
                f"market is on exchange shard {shard}; execution is configured for shard "
                f"{self.exchange_index}"
            )
        return dict(market)

    async def quote(self, ticker: str, series: str) -> dict[str, Any]:
        started = self.clock()
        market = await self.market(ticker)
        event = (await self.get(f"/events/{market['event_ticker']}"))["event"]
        if event["series_ticker"] != series:
            raise GuardError("market series mismatch")
        info = (await self.get(f"/series/{series}"))["series"]
        changes = (await self.get("/series/fee_changes", {"series_ticker": series}))[
            "series_fee_change_arr"
        ]
        event_changes = await self.pages(
            "/events/fee_changes",
            "event_fee_changes",
            {"event_ticker": market["event_ticker"]},
        )
        for change in [*changes, *event_changes]:
            if started <= timestamp(change["scheduled_ts"]) <= started + 60_000:
                raise GuardError("fee schedule changes within confirmation window")
        for source, type_key, multiplier_key in [
            (info, "fee_type", "fee_multiplier"),
            (event, "fee_type_override", "fee_multiplier_override"),
        ]:
            fee_type = source.get(type_key)
            multiplier = source.get(multiplier_key)
            if source is event and fee_type is None and multiplier is None:
                continue
            if fee_type not in {"quadratic", "quadratic_with_maker_fees"} or multiplier is None:
                raise GuardError("unsupported or unknown fee schedule")
            amount = Decimal(str(multiplier))
            if not amount.is_finite() or not 0 <= amount <= 1:
                raise GuardError("fee schedule exceeds reserved bound")
        book = (await self.get(f"/markets/{ticker}/orderbook"))["orderbook_fp"]
        best: dict[str, tuple[int, int]] = {}
        for side in ("yes", "no"):
            levels = book[f"{side}_dollars"]
            valid = [
                (fixed(level[0], DOLLAR), fixed(level[1], 100))
                for level in levels
                if fixed(level[1], 100) > 0
            ]
            if not valid:
                raise GuardError("both book sides must be quoted")
            best[side] = max(valid)
        if not 0 < best["yes"][0] < DOLLAR or not 0 < best["no"][0] < DOLLAR:
            raise GuardError("invalid book price")
        if best["yes"][0] + best["no"][0] >= DOLLAR:
            raise GuardError("crossed book")
        return {"market": market, "bids": best, "observed_ms": started}

    async def submit(
        self,
        intent: Intent,
        client_id: str,
        deadline_ms: int | None = None,
        resting_until_s: int | None = None,
    ) -> dict[str, Any]:
        """IOC by default. With resting_until_s: a post-only good-till-canceled order that
        Kalshi expires at that unix second even if this host is gone."""
        url = self.base + "/portfolio/events/orders"
        body: dict[str, Any] = {
            "ticker": intent.ticker,
            "client_order_id": client_id,
            "side": intent.book_side,
            "count": intent.quantity,
            "price": f"{Decimal(dollars(intent.yes_price)):.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": intent.action == "sell",
            "cancel_order_on_pause": True,
            "subaccount": self.subaccount,
            "exchange_index": self.exchange_index,
        }
        if resting_until_s is not None:
            if intent.action != "buy":
                raise BrokerRejectedError("only buys may rest", dispatched=False)
            body.update(
                time_in_force="good_till_canceled",
                post_only=True,
                expiration_ts=resting_until_s,
            )
        await self.write_bucket.acquire()
        if deadline_ms is not None and self.clock() >= deadline_ms:
            raise BrokerRejectedError(
                "local data deadline expired before broker dispatch", dispatched=False
            )
        response = await self.client.post(url, json=body, headers=self.signer.headers("POST", url))
        if response.status_code in {400, 401, 403, 404, 422}:
            raise BrokerRejectedError(f"broker rejected order ({response.status_code})")
        response.raise_for_status()
        receipt = response.json()
        if not isinstance(receipt, dict) or not isinstance(receipt.get("order_id"), str):
            raise GuardError("invalid submission receipt; reconciliation required")
        if receipt.get("client_order_id", client_id) != client_id:
            raise GuardError("submission receipt client id mismatch")
        return receipt

    async def cancel(self, order_id: str) -> None:
        """Cancel one resting order. Already gone (404) is fine; reconciliation reads the
        final status and fills either way."""
        url = f"{self.base}/portfolio/events/orders/{order_id}"
        await self.write_bucket.acquire()
        response = await self.client.delete(
            url,
            params={"subaccount": self.subaccount, "exchange_index": self.exchange_index},
            headers=self.signer.headers("DELETE", url),
        )
        if response.status_code != 404:
            response.raise_for_status()

    async def find_order(self, client_id: str, order_id: str | None) -> dict[str, Any] | None:
        params = {"subaccount": self.subaccount, "exchange_index": self.exchange_index}
        if order_id:
            try:
                return dict((await self.get(f"/portfolio/orders/{order_id}", params))["order"])
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise
                # A just-acknowledged order may reach the list before the ID read.
        rows = await self.pages(
            "/portfolio/orders",
            "orders",
            params,
        )
        matches = [row for row in rows if row.get("client_order_id") == client_id]
        if len(matches) > 1:
            raise GuardError("duplicate client order ids at broker")
        if matches and order_id and matches[0].get("order_id") != order_id:
            raise GuardError("broker order id mismatch")
        if not matches and order_id:
            raise GuardError("acknowledged broker order not yet visible")
        return matches[0] if matches else None

    async def order_fills(self, order: dict[str, Any], intent: Intent) -> list[dict[str, Any]]:
        if (
            order["ticker"] != intent.ticker
            or order["book_side"] != intent.book_side
            or fixed(order["initial_count_fp"], 100) != intent.units
            or fixed(order["yes_price_dollars"], DOLLAR) != intent.yes_price
            or order["subaccount_number"] != self.subaccount
            or order.get("exchange_index", self.exchange_index) != self.exchange_index
        ):
            raise GuardError("broker order differs from confirmed intent")
        rows = await self.pages(
            "/portfolio/fills",
            "fills",
            {
                "order_id": order["order_id"],
                "subaccount": self.subaccount,
                "exchange_index": self.exchange_index,
            },
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            if (
                row["order_id"] != order["order_id"]
                or row["ticker"] != intent.ticker
                or row["book_side"] != intent.book_side
                or row["subaccount_number"] != self.subaccount
                or row.get("exchange_index", self.exchange_index) != self.exchange_index
            ):
                raise GuardError("broker fill identity mismatch")
            units = fixed(row["count_fp"], 100)
            yes_price = fixed(row["yes_price_dollars"], DOLLAR)
            if not 0 <= yes_price <= DOLLAR:
                raise GuardError("invalid fill price")
            price = yes_price if intent.side == "yes" else DOLLAR - yes_price
            out.append(
                {
                    "id": row["fill_id"],
                    "units": units,
                    "cost": price * units // 100,
                    "fee": fixed(row["fee_cost"], DOLLAR),
                    "ts": timestamp(row["created_time"]),
                    "yes_price": yes_price,
                    "is_taker": row["is_taker"],
                }
            )
        if len({fill["id"] for fill in out}) != len(out):
            raise GuardError("duplicate broker fills")
        if sum(fill["units"] for fill in out) != fixed(order["fill_count_fp"], 100):
            raise GuardError("fills not yet consistent with order")
        if sum(fill["fee"] for fill in out) != (
            fixed(order["taker_fees_dollars"], DOLLAR) + fixed(order["maker_fees_dollars"], DOLLAR)
        ):
            raise GuardError("fill fees not yet consistent with order")
        if (
            order["status"] in {"canceled", "executed"}
            and fixed(order["remaining_count_fp"], 100) != 0
        ):
            raise GuardError("terminal order has remaining contracts")
        return sorted(out, key=lambda fill: (fill["ts"], fill["id"]))
