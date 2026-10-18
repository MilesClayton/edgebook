"""Score candidate long-shot rule changes found in the gap analysis, one entry per market."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from edgebook.research.longshot_gaps import (
    STUDY_CONFIG,
    chrono_split,
    firings,
    first_per_market,
    summary,
)

rows = firings(Path("history"), STUDY_CONFIG)
tr, va, te = chrono_split(rows)
periods = {"train_val": tr + va, "test": te}
rules = {
    "current_rule": lambda f: True,
    "hourly_only": lambda f: f["hourly"] == 1,
    "15min_plus_left": lambda f: f["minutes_left"] >= 15,
    "hourly_and_15min_plus": lambda f: f["hourly"] == 1 and f["minutes_left"] >= 15,
    "hourly_15min_plus_ask_le_6c": lambda f: (
        f["hourly"] == 1 and f["minutes_left"] >= 15 and f["long_ask"] < 0.07
    ),
    "hourly_15min_plus_z_ge_1_5": lambda f: (
        f["hourly"] == 1 and f["minutes_left"] >= 15 and f["z_to_strike"] >= 1.5
    ),
}
out: dict[str, dict[str, object]] = {}
for name, rule in rules.items():
    out[name] = {}
    for period, idx in periods.items():
        sel = [rows[j] for j in idx]
        keep = np.array([rule(r.features) for r in sel])
        out[name][period] = summary(first_per_market(sel, keep))
        out[name][period + "_all_firings_unfilled_too"] = summary(
            [r for r, k in zip(sel, keep, strict=True) if k]
        )
print(json.dumps(out, indent=1))
