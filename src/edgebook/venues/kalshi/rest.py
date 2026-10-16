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
from edgebook.venues.kalshi.models import Balance, MarketsPage, RestMarket
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

    async def balance(self) -> Balance:
        return Balance.model_validate(await self._get("/portfolio/balance", signed=True))
