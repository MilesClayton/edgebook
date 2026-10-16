"""Small defensive helpers shared by the public-feed parsers."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any


def decode_object(message: str | bytes) -> dict[str, Any] | None:
    try:
        decoded = json.loads(message)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def number(value: object) -> float | None:
    if not isinstance(value, int | float | str):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def timestamp_ms(value: object, fallback_ms: int) -> int:
    """Parse epoch seconds/milliseconds or an RFC3339 timestamp."""
    if isinstance(value, int | float):
        stamp = float(value)
        if not math.isfinite(stamp):
            return fallback_ms
        return int(stamp if stamp >= 100_000_000_000 else stamp * 1000)
    if not isinstance(value, str) or not value:
        return fallback_ms
    try:
        stamp = float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return fallback_ms
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)
    if not math.isfinite(stamp):
        return fallback_ms
    return int(stamp if stamp >= 100_000_000_000 else stamp * 1000)
