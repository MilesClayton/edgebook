"""What the long-shot pilot may rest right now: the paper long-shot seller's current sales."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from edgebook.engine.decide import Call
from edgebook.engine.loop import LONGSHOT_DATA_GATES, LONGSHOT_EVERY_MS
from edgebook.engine.maker import LongshotConfig, longshot_quote
from edgebook.engine.state import MarketInfo
from edgebook.execution.live_ledger import money


def longshot_quotes(
    calls: Iterable[Call],
    markets: Mapping[str, MarketInfo],
    cfg: LongshotConfig,
    series: Collection[str],
    now_ms: int,
    last_tick_ms: int,
    max_index_age_ms: int,
) -> list[dict[str, Any]]:
    """One quote per market where the paper strategy would sell a long shot, using the same
    rule (longshot_quote) and data checks. Stale engine output yields nothing."""
    if not 0 <= now_ms - last_tick_ms <= 2 * LONGSHOT_EVERY_MS:
        return []
    out = []
    for call in calls:
        market = markets.get(call.ticker)
        if (
            market is None
            or call.series not in series
            or not all(call.gates.get(k, False) for k in LONGSHOT_DATA_GATES)
            or call.index_age_ms is None
            or not 0 <= call.index_age_ms + now_ms - last_tick_ms <= max_index_age_ms
        ):
            continue
        quote = longshot_quote(call, cfg)
        if quote is None:
            continue
        side, price, fair = quote
        out.append(
            {
                "ticker": call.ticker,
                "series": call.series,
                "side": side,
                "price": money(f"{price:.2f}"),
                "fair": fair,
                "seconds_left": call.seconds_left,
                "close_ms": market.close_ts * 1000,
            }
        )
    return out
