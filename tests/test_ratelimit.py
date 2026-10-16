import pytest

from edgebook.venues.kalshi.ratelimit import TokenBucket


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


async def test_bucket_paces_to_rate() -> None:
    t = FakeTime()
    bucket = TokenBucket(200, clock=t.clock, sleep=t.sleep)  # Basic tier: 20 req/s
    for _ in range(40):
        await bucket.acquire()
    # First 20 are free (full bucket), next 20 need 1s of refill.
    assert t.now == pytest.approx(1.0)


async def test_bucket_rejects_impossible_cost_and_bad_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(0)
    with pytest.raises(ValueError, match="capacity"):
        await TokenBucket(5).acquire(10)
