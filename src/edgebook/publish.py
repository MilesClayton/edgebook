"""Push JSON to the dashboard store served by the Fly recorder (``recorder/http.py``).

Only model output, market data and the bot's own trade log are published;
never balances, keys or account identifiers. Values are made strict-JSON safe
(NaN/inf -> null) because browsers reject Python's NaN literals.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import httpx

from edgebook.config import Settings

log = logging.getLogger(__name__)
KINDS = ("snapshot", "research", "calibration", "trades")


def clean(x: Any) -> Any:
    """Recursively convert to strict-JSON-safe primitives."""
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return clean(dataclasses.asdict(x))
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [clean(v) for v in x]
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, Decimal):
        return float(x) if x.is_finite() else None
    if isinstance(x, datetime | date):
        return x.isoformat()
    if isinstance(x, Enum):
        return x.value
    if hasattr(x, "item"):  # numpy scalars
        return clean(x.item())
    return x


async def publish(
    settings: Settings, kind: str, data: Any, client: httpx.AsyncClient | None = None
) -> bool:
    """Returns False (and logs) instead of raising: the dashboard is never critical path."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if not settings.dashboard_url or settings.dashboard_token is None:
        log.info("dashboard not configured; skipping %s publish", kind)
        return False
    own = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await client.post(
            f"{settings.dashboard_url.rstrip('/')}/dashboard/{kind}",
            json={"data": clean(data)},
            headers={"Authorization": f"Bearer {settings.dashboard_token.get_secret_value()}"},
        )
        if resp.status_code != 200:
            log.warning(
                "dashboard %s publish failed: %s %s", kind, resp.status_code, resp.text[:200]
            )
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("dashboard %s publish failed: %s", kind, exc)
        return False
    finally:
        if own:
            await client.aclose()
