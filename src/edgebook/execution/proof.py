"""Paper proof required before real-money entries.

A strategy may route real orders only after its own paper account shows
profit that is unlikely to be luck: enough closed trades, spread over enough
separate days (one lucky session is not evidence), with the 95% confidence
interval of profit per contract entirely above zero.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
from scipy.stats import t

from edgebook.model.calibration import MeanCI, mean_ci


def paper_proof(
    closed: Sequence[tuple[float, float, int]], min_trades: int, min_days: int
) -> dict[str, Any]:
    """``closed`` is a paper account's (pnl, contracts, closed_ms) record."""
    # Daily blocks conservatively group correlated strikes and partial exits.
    # The saved legacy record lacks event IDs, so do not pretend each fill is
    # independent evidence. Report trade count separately from inference units.
    valid = [(pnl, n, ts) for pnl, n, ts in closed if n > 0]
    blocks: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for pnl, n, ts in valid:
        if ts > 0:
            key = datetime.fromtimestamp(ts / 1000, tz=UTC).date().isoformat()
            blocks[key][0] += pnl
            blocks[key][1] += n
    per_contract = [pnl / n for pnl, n in blocks.values()]
    days = {
        datetime.fromtimestamp(ts / 1000, tz=UTC).date() for _, n, ts in closed if n > 0 and ts > 0
    }
    ci = mean_ci(np.array(per_contract, dtype=float))
    if ci.n > 1:
        half = float(t.ppf(0.975, ci.n - 1)) * float(np.std(per_contract, ddof=1)) / ci.n**0.5
        ci = MeanCI(ci.n, ci.mean, ci.mean - half, ci.mean + half)
    out: dict[str, Any] = {
        "trades": len(valid),
        "independent_blocks": len(blocks),
        "confidence_method": "daily blocks",
        "days": len(days),
        "mean_per_contract": ci.mean if ci.n else None,
        "ci_lo": ci.ci_lo if ci.n > 1 else None,
        "ci_hi": ci.ci_hi if ci.n > 1 else None,
        "min_trades": min_trades,
        "min_days": min_days,
    }
    if len(valid) < min_trades:
        reason = f"{len(valid)} closed paper trades; {min_trades} required"
    elif len(days) < min_days:
        reason = f"paper trades span {len(days)} days; {min_days} required"
    elif not ci.ci_lo > 0:
        reason = (
            f"profit per contract {ci.mean * 100:+.2f}c with 95% range "
            f"{ci.ci_lo * 100:+.2f} to {ci.ci_hi * 100:+.2f}c does not exclude zero or worse"
        )
    else:
        reason = "paper profit is above zero at 95% confidence"
    out["proven"] = reason == "paper profit is above zero at 95% confidence"
    out["reason"] = reason
    return out
