"""Venue-agnostic interfaces the recorder, strategy and backtest depend on."""

from __future__ import annotations

from typing import Protocol

from edgebook.core.types import Market


class MarketCatalog(Protocol):
    """Discovers tradable markets for a series (e.g. a strike ladder per hour)."""

    async def open_markets(self, series: str) -> list[Market]: ...
