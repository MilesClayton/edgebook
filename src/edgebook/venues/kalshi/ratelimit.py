"""Token bucket matching Kalshi's per-tier read/write budgets.

Kalshi budgets are tokens per second with a default request cost of 10, so
Basic = 200 read tokens/s = 20 requests/s. 429s carry no retry headers, so the
client paces itself here rather than relying on the server.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

DEFAULT_COST = 10
EPSILON = 1e-9
MIN_SLEEP_S = 1e-3


class TokenBucket:
    def __init__(
        self,
        rate_per_s: float,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate_per_s
        self.capacity = capacity if capacity is not None else rate_per_s
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    async def acquire(self, cost: float = DEFAULT_COST) -> None:
        if cost > self.capacity:
            raise ValueError(f"cost {cost} exceeds bucket capacity {self.capacity}")
        async with self._lock:
            self._refill()
            while self._tokens < cost - EPSILON:
                await self._sleep(max((cost - self._tokens) / self.rate, MIN_SLEEP_S))
                self._refill()
            # Float refill can land a hair under cost; never let the bucket go negative.
            self._tokens = max(0.0, self._tokens - cost)
