"""Async Kalshi REST client.

Market data endpoints are public; portfolio endpoints are signed. All calls
pass through the read token bucket so the client never trips a 429.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from edgebook.core.types import Market
from edgebook.venues.kalshi.auth import KalshiSigner
from edgebook.venues.kalshi.models import (
    Balance,
    BatchGetMarketCandlesticksResponse,
    CreateOrderV2Request,
    CreateOrderV2Response,
    GetMarketResponse,
    GetTradesResponse,
    MarketCandlestick,
    MarketsPage,
    PublicTrade,
    RestMarket,
)
from edgebook.venues.kalshi.ratelimit import TokenBucket

# Kalshi series -> underlying asset. Extending to ETH/SOL is a config entry.
SERIES_UNDERLYING: Mapping[str, str] = {
    "KXBTCD": "BTC",
    "KXBTC": "BTC",
    "KXBTC15M": "BTC",
    "KXETHD": "ETH",
    "KXETH": "ETH",
    "KXETH15M": "ETH",
}
BASIC_READ_TOKENS_PER_S = 200
BASIC_WRITE_TOKENS_PER_S = 100
CANDLE_BATCH = 100  # Kalshi max tickers per batch candlestick request
MAX_RETRIES = 5


def underlying_for(series: str) -> str:
    try:
        return SERIES_UNDERLYING[series]
    except KeyError:
        raise KeyError(f"unknown series {series!r}; add it to SERIES_UNDERLYING") from None


class KalshiRest:
    def __init__(
        self,
        base_url: str,
        signer: KalshiSigner | None = None,
        client: httpx.AsyncClient | None = None,
        bucket: TokenBucket | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._signer = signer
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._bucket = bucket or TokenBucket(BASIC_READ_TOKENS_PER_S)
        self._write_bucket = TokenBucket(BASIC_WRITE_TOKENS_PER_S)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(
        self, path: str, params: Mapping[str, str | int] | None = None, signed: bool = False
    ) -> Any:
        url = f"{self._base}{path}"
        for attempt in range(MAX_RETRIES):
            await self._bucket.acquire()
            headers: dict[str, str] = {}
            if signed:
                if self._signer is None:
                    raise RuntimeError(f"{path} requires an API key")
                headers = self._signer.headers("GET", url)
            resp = await self._client.get(url, params=params, headers=headers)
            if resp.status_code == httpx.codes.TOO_MANY_REQUESTS or resp.status_code >= 500:
                await asyncio.sleep(min(0.5 * 2**attempt, 8.0))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"GET {path} failed after {MAX_RETRIES} attempts")

    async def markets(
        self,
        series: str,
        status: str | None = "open",
        min_close: datetime | None = None,
    ) -> list[RestMarket]:
        params: dict[str, str | int] = {"series_ticker": series, "limit": 1000}
        if status is not None:
            params["status"] = status
        if min_close is not None:
            params["min_close_ts"] = int(min_close.timestamp())
        out: list[RestMarket] = []
        while True:
            page = MarketsPage.model_validate(await self._get("/markets", params))
            out.extend(page.markets)
            if not page.cursor or not page.markets:
                return out
            params["cursor"] = page.cursor

    async def open_markets(self, series: str) -> list[Market]:
        underlying = underlying_for(series)
        return [m.to_core(underlying) for m in await self.markets(series)]

    async def market(self, ticker: str) -> RestMarket:
        """Fetch one market, including its settled result when it has closed."""
        return GetMarketResponse.model_validate(await self._get(f"/markets/{ticker}")).market

    async def candlesticks(
        self, tickers: list[str], start: datetime, end: datetime, period_min: int = 1
    ) -> dict[str, list[MarketCandlestick]]:
        """Bid/ask candles per ticker (public). Batches of 100 tickers."""
        out: dict[str, list[MarketCandlestick]] = {}
        for i in range(0, len(tickers), CANDLE_BATCH):
            params: dict[str, str | int] = {
                "market_tickers": ",".join(tickers[i : i + CANDLE_BATCH]),
                "start_ts": int(start.timestamp()),
                "end_ts": int(end.timestamp()),
                "period_interval": period_min,
            }
            page = BatchGetMarketCandlesticksResponse.model_validate(
                await self._get("/markets/candlesticks", params)
            )
            for m in page.markets:
                out[m.market_ticker] = m.candlesticks
        return out

    async def market_trades(self, ticker: str) -> list[PublicTrade]:
        """Every public trade in a market (paginated, public)."""
        params: dict[str, str | int] = {"ticker": ticker, "limit": 1000}
        out: list[PublicTrade] = []
        while True:
            page = GetTradesResponse.model_validate(await self._get("/markets/trades", params))
            out.extend(page.trades)
            if not page.cursor or not page.trades:
                return out
            params["cursor"] = page.cursor

    async def create_order(self, order: CreateOrderV2Request) -> CreateOrderV2Response:
        """Place an order (signed). Never retried: a timeout may still have placed it."""
        if self._signer is None:
            raise RuntimeError("placing orders requires an API key")
        url = f"{self._base}/portfolio/events/orders"
        await self._write_bucket.acquire()
        resp = await self._client.post(
            url, json=order.model_dump(), headers=self._signer.headers("POST", url)
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"order rejected ({resp.status_code}): {resp.text[:300]}")
        return CreateOrderV2Response.model_validate(resp.json())

    async def balance(self) -> Balance:
        return Balance.model_validate(await self._get("/portfolio/balance", signed=True))
