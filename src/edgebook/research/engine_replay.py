"""Replay recorded market data through the *live* engine and its paper accounts.

The tick-replay research (``research.tick``) tests standalone strategies; this
drives the exact production code path instead: ``LiveState`` is rebuilt from the
recorder's Parquet streams (books, public trades, BRTI ticks) in arrival order,
``LiveEngine.tick()`` runs once per second of data time, and held markets settle
on their recorded results. It answers "would these strategies have made money on
data we already have?" and checks that the simulator reproduces what the live
paper account actually did.

Streams are read file by file and batch by batch so a day of data (tens of
millions of book rows) fits in modest memory. The exchange composite is not
replayed, so the "exchanges in line with index" check always passes here.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.dataset as ds

from edgebook.core.book import BookState
from edgebook.core.types import Side
from edgebook.engine.loop import LiveEngine, StrategySpec
from edgebook.engine.state import INDEX_KEEP_S, LiveState, MarketInfo

SERIES_LEN_S = {"KXBTC15M": 900, "KXBTCD": 3600}


@dataclass(frozen=True)
class ReplayMarket:
    info: MarketInfo
    result: str


def load_markets(directory: Path) -> dict[str, ReplayMarket]:
    out: dict[str, ReplayMarket] = {}
    for stream in ("markets", "settlements"):
        path = directory / stream
        if not path.exists():
            continue
        cols = [
            "series",
            "ticker",
            "close_time",
            "strike_type",
            "floor_strike",
            "cap_strike",
            "result",
        ]
        for r in ds.dataset(path, partitioning="hive").to_table(columns=cols).to_pylist():
            prev = out.get(r["ticker"])
            close_ts = int(r["close_time"].timestamp())
            info = (
                prev.info
                if prev
                else MarketInfo(
                    ticker=r["ticker"],
                    series=r["series"],
                    open_ts=close_ts - SERIES_LEN_S.get(r["series"], 3600),
                    close_ts=close_ts,
                    strike_type=r["strike_type"] or "",
                    floor=float(r["floor_strike"]) if r["floor_strike"] is not None else None,
                    cap=float(r["cap_strike"]) if r["cap_strike"] is not None else None,
                )
            )
            result = r["result"] if r["result"] in ("yes", "no") else (prev.result if prev else "")
            out[r["ticker"]] = ReplayMarket(info, result)
    return out


def _stream(
    directory: Path, name: str, columns: list[str], start_ms: int, end_ms: int
) -> Iterator[tuple[int, int, str, dict[str, Any]]]:
    path = directory / name
    if not path.exists():
        return
    flt = (ds.field("recv_ts_ms") >= start_ms) & (ds.field("recv_ts_ms") < end_ms)
    files = sorted(str(f) for f in path.rglob("*.parquet") if f.stat().st_size > 0)
    # files are written in arrival order; sort within each file to be safe
    for i, f in enumerate(files):
        try:
            tbl = ds.dataset(f).to_table(columns=columns, filter=flt)
        except (pa.ArrowInvalid, OSError):
            continue  # an empty/torn part file left by a restart mid-write
        if tbl.num_rows == 0:
            continue
        tbl = tbl.sort_by([("recv_ts_ms", "ascending")])
        for j, r in enumerate(tbl.to_pylist()):
            yield r["recv_ts_ms"], i * 10_000_000 + j, name, r


def events(
    directory: Path, start_ms: int, end_ms: int
) -> Iterator[tuple[int, int, str, dict[str, Any]]]:
    """(recv_ms, order, stream, row) merged across book/trades/index by arrival time."""
    return heapq.merge(
        _stream(
            directory,
            "book",
            ["recv_ts_ms", "kind", "ticker", "side", "price", "qty"],
            start_ms,
            end_ms,
        ),
        _stream(
            directory,
            "trades",
            ["recv_ts_ms", "ticker", "yes_price", "count", "taker_side"],
            start_ms,
            end_ms,
        ),
        _stream(directory, "index", ["recv_ts_ms", "index_id", "raw"], start_ms, end_ms),
        key=lambda e: (e[0], e[1]),
    )


def _apply_book(state: LiveState, r: dict[str, Any]) -> None:
    """A book becomes usable only at a recorded snapshot ("reset" + its levels), so a
    replay that starts mid-stream never trades on a half-known book."""
    tk = r["ticker"]
    if r["kind"] == "reset":
        fresh = state.books[tk] = BookState(tk)
        fresh.apply_snapshot([], [])
        return
    book = state.books.get(tk)
    if book is None or not book.ready:
        return
    try:
        book.apply_delta(Side(r["side"]), Decimal(str(r["price"])), Decimal(str(r["qty"])))
    except ValueError:
        book.reset()  # inconsistent: unusable until the next snapshot


@dataclass(frozen=True)
class ReplayResult:
    start_ms: int
    end_ms: int
    ticks: int
    accounts: list[dict[str, Any]]  # strategy payloads at the end, after settling


def replay(
    directory: Path,
    accounts_dir: Path,
    strategies: Sequence[StrategySpec],
    start_ms: int,
    end_ms: int,
    tick_ms: int = 1000,
) -> ReplayResult:
    markets = load_markets(directory)
    state = LiveState()
    state.markets = {tk: m.info for tk, m in markets.items()}
    clock = [start_ms]
    engine = LiveEngine(
        state,
        accounts_dir / "paper.json",
        clock=lambda: clock[0],
        strategies=strategies,
    )
    next_tick = start_ms + tick_ms
    ticks = 0

    def run_until(t: int) -> None:
        nonlocal next_tick, ticks
        while next_tick <= t:
            clock[0] = next_tick
            engine.tick()
            ticks += 1
            for tk in engine.pending_settlement:
                res = markets.get(tk)
                if res is not None and res.result and res.info.close_ts * 1000 <= next_tick:
                    engine.settle(tk, res.result)
            next_tick += tick_ms

    for recv_ms, _, stream, r in events(directory, start_ms, end_ms):
        run_until(recv_ms)
        if stream == "book":
            state.ws_last_ms = recv_ms
            _apply_book(state, r)
        elif stream == "trades":
            state.ws_last_ms = recv_ms
            state.add_trade(
                r["ticker"], float(r["yes_price"]), float(r["count"]), r["taker_side"], recv_ms
            )
        elif r["index_id"] == state.index_id:
            try:
                value = float(json.loads(r["raw"])["value"])
            except (ValueError, KeyError, TypeError):
                continue
            state.ws_last_ms = recv_ms
            state.index.append((recv_ms, value))
            while state.index and state.index[0][0] < recv_ms - INDEX_KEEP_S * 1000:
                state.index.popleft()
    run_until(end_ms)
    for runner in engine.runners:  # settle whatever closed inside the window
        for tk in list(runner.account.holdings):
            res = markets.get(tk)
            if res is not None and res.result:
                runner.account.settle(tk, res.result, end_ms)
    accounts = []
    for runner in engine.runners:
        payload = engine.paper_payload(runner.spec.name)
        payload["fills"] = [
            asdict(f) for f in runner.account.fills
        ]  # all of them, not the last 300
        accounts.append(payload)
    return ReplayResult(start_ms, end_ms, ticks, accounts)
