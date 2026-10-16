from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from edgebook.venues.kalshi.auth import KalshiSigner


@pytest.fixture
def signer() -> KalshiSigner:
    return KalshiSigner("kid", ed25519.Ed25519PrivateKey.generate(), clock_ms=lambda: 1)


def market(ticker: str, **kw: Any) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "event_ticker": "KXBTCD-26OCT1602",
        "status": "active",
        "close_time": "2026-10-16T06:00:00Z",
        "strike_type": "greater",
        "floor_strike": 90299.99,
        **kw,
    }
