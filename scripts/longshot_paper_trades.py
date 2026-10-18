"""Break down the long-shot paper account's actual trades by every range.

Usage: uv run python scripts/longshot_paper_trades.py path/to/paper-longshot.json

Each settled market is one trade (the strategy holds to the result): its buys are summed,
its settlement P&L is taken from the account, and per-contract P&L is P&L / contracts.
Intervals resample whole closes, since every strike of one hour moves together.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from edgebook.research.longshot_gaps import event_ci

ET = timedelta(hours=-4)  # Kalshi tickers use US Eastern time (EDT in October)
FAIR = re.compile(r"fair value ([0-9.]+)")


def close_of(ticker: str) -> datetime:
    """KXBTCD-26OCT1806-T... closes 06:00 ET; KXBTC15M-26OCT180530-30 closes 05:30 ET."""
    stamp = ticker.split("-")[1]
    day = datetime.strptime(stamp[:7], "%y%b%d").replace(tzinfo=UTC)  # a date; ET applied below
    hour, minute = int(stamp[7:9]), int(stamp[9:11]) if len(stamp) > 9 else 0
    return day + timedelta(hours=hour, minutes=minute) - ET


def trades(account: dict[str, Any]) -> list[dict[str, Any]]:
    buys: dict[str, list[dict[str, Any]]] = defaultdict(list)
    settled: dict[str, dict[str, Any]] = {}
    for f in account["fills"]:
        if f["action"] == "BUY":
            buys[f["ticker"]].append(f)
        elif f["action"] == "SETTLE":
            settled[f["ticker"]] = f
    out = []
    for tk, s in settled.items():
        b = buys.get(tk)
        if not b:
            continue  # bought before the saved fill window
        contracts = sum(f["contracts"] for f in b)
        price = sum(f["price"] * f["contracts"] for f in b) / contracts
        first = min(f["ts_ms"] for f in b)
        fair = [float(m.group(1)) for f in b if (m := FAIR.search(f["reason"]))]
        close = close_of(tk)
        out.append(
            {
                "ticker": tk,
                "event": tk.rsplit("-", 1)[0],
                "series": tk.split("-")[0],
                "sold": "UP" if b[0]["side"] == "no" else "DOWN",
                "long_price": 1 - price,
                "minutes_left": (close.timestamp() * 1000 - first) / 60_000,
                "fair_minus_price": (np.mean(fair) - price) if fair else float("nan"),
                "hour_et": (close + ET).hour,
                "contracts": contracts,
                "pnl": s["pnl"],
                "pnl_per_contract": s["pnl"] / s["contracts"],
                "long_shot_won": s["pnl"] < 0,
                "fills": len(b),
            }
        )
    return sorted(out, key=lambda t: t["ticker"])


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pc = np.array([r["pnl_per_contract"] for r in rows])
    ev = np.array([r["event"] for r in rows])
    lo, hi = event_ci(pc, ev)
    contracts = sum(r["contracts"] for r in rows)
    return {
        "trades": len(rows),
        "closes": len(set(ev.tolist())),
        "losses": sum(r["long_shot_won"] for r in rows),
        "mean_cents": float(pc.mean() * 100),
        "ci_cents": [lo * 100, hi * 100],
        "dollar_weighted_cents": sum(r["pnl"] for r in rows) / contracts * 100,
        "pnl_dollars": sum(r["pnl"] for r in rows),
    }


RANGES: dict[str, tuple[str, list[tuple[str, Any]]]] = {
    "series": (
        "series",
        [("15-minute", "KXBTC15M"), ("hourly above/below", "KXBTCD"), ("hourly range", "KXBTC")],
    ),
    "long_price": (
        "long_price",
        [
            ("<2c", (0, 0.02)),
            ("2-4c", (0.02, 0.04)),
            ("4-7c", (0.04, 0.07)),
            ("7-10c", (0.07, 0.10)),
            ("10-15c", (0.10, 0.155)),
            (">15c", (0.155, 1)),
        ],
    ),
    "minutes_left": (
        "minutes_left",
        [
            ("<15", (0, 15)),
            ("15-30", (15, 30)),
            ("30-45", (30, 45)),
            ("45-60", (45, 61)),
            (">60", (61, 1e9)),
        ],
    ),
    "sold": ("sold", [("sold UP", "UP"), ("sold DOWN", "DOWN")]),
    "fair_minus_price": (
        "fair_minus_price",
        [("fair <= price", (-1, 0.0001)), ("fair > price", (0.0001, 1))],
    ),
}


def table(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for name, (key, buckets) in RANGES.items():
        out[name] = []
        for label, cond in buckets:
            sel = [
                r
                for r in rows
                if (cond[0] <= r[key] < cond[1] if isinstance(cond, tuple) else r[key] == cond)
            ]
            if sel:
                out[name].append({"range": label, **stats(sel)})
    return out


def main() -> None:
    account = json.loads(Path(sys.argv[1]).read_text())
    rows = trades(account)
    hourly = [r for r in rows if r["series"] == "KXBTCD"]
    result = {
        "first_trade_close": min(close_of(r["ticker"]) for r in rows).isoformat(),
        "last_trade_close": max(close_of(r["ticker"]) for r in rows).isoformat(),
        "settled_in_file": len(rows),
        "open_positions": len(account["holdings"]),
        "all": stats(rows),
        "by_range_all": table(rows),
        "hourly": stats(hourly),
        "by_range_hourly": table(hourly),
        "hourly_by_close": [
            {"close": ev, **stats([r for r in hourly if r["event"] == ev])}
            for ev in sorted({r["event"] for r in hourly})
        ],
    }
    print(json.dumps(result, indent=1, default=str))


if __name__ == "__main__":
    main()
