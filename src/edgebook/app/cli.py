"""Command line entrypoints.

python -m edgebook record    # stream prod market data to Parquet (needs a prod key)
python -m edgebook balance   # signed call to verify a key against EDGEBOOK_ENV
python -m edgebook markets   # list open markets per series (public, no key)
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from edgebook.config import KALSHI_URLS, Settings, assert_live_trading_allowed
from edgebook.core.fees import trade_fee
from edgebook.core.types import LiquidityRole
from edgebook.execution.models import ExecutionMode, SessionConfig, SessionStatus
from edgebook.execution.paper import PaperRiskError
from edgebook.execution.runner import PaperSession, paper_store
from edgebook.history import coinbase
from edgebook.history import kalshi as kalshi_history
from edgebook.live.signals import LivePricer, Signal, signals
from edgebook.live.snapshot import SnapshotBuilder
from edgebook.publish import clean, publish
from edgebook.recorder import http as dashboard_http
from edgebook.recorder.run import Recorder
from edgebook.recorder.sink import ParquetSink
from edgebook.research import calibrate as calib
from edgebook.research import features, strategies
from edgebook.venues.kalshi.auth import KalshiSigner
from edgebook.venues.kalshi.models import CreateOrderV2Request
from edgebook.venues.kalshi.rest import KalshiRest
from edgebook.venues.kalshi.ws import KALSHI_WS_URLS, KalshiWebSocket

log = logging.getLogger(__name__)


def signer_from(settings: Settings) -> KalshiSigner:
    pem = settings.kalshi_pem()
    if settings.kalshi_key_id is None or pem is None:
        raise SystemExit(
            "set EDGEBOOK_KALSHI_KEY_ID and EDGEBOOK_KALSHI_PRIVATE_KEY "
            "(or EDGEBOOK_KALSHI_PRIVATE_KEY_PATH)"
        )
    return KalshiSigner.from_pem(settings.kalshi_key_id, pem)


async def record(settings: Settings) -> None:
    # Market data is always production, whatever EDGEBOOK_ENV says; no orders here.
    signer = signer_from(settings)
    recorder = Recorder(
        rest=KalshiRest(settings.kalshi_market_data_url),
        ws=KalshiWebSocket(KALSHI_WS_URLS["production"], signer),
        sink=ParquetSink(settings.data_dir),
        series=settings.record_series,
        index_ids=settings.record_indices,
    )
    if settings.dashboard_token is not None:
        store = settings.data_dir / "dashboard"
        await dashboard_http.serve(
            store, settings.dashboard_token.get_secret_value(), settings.dashboard_port
        )
        builder = SnapshotBuilder(
            rest=recorder.rest,
            pricer=LivePricer(settings.data_dir / "history"),
            recorder=recorder,
            data_dir=settings.data_dir,
        )
        paper_session = PaperSession(paper_store(settings.data_dir))
        last_control_id: str | None = None

        async def apply_paper_control() -> None:
            nonlocal last_control_id
            control_path = store / "control.json"
            if control_path.is_file():
                try:
                    command = json.loads(control_path.read_text()).get("data", {})
                    control_id = command.get("id")
                    if control_id and control_id != last_control_id:
                        action = command.get("action")
                        if action == "start":
                            config = SessionConfig.model_validate(command.get("config", {}))
                            if config.mode is not ExecutionMode.PAPER:
                                raise ValueError("only paper sessions can be controlled here")
                            try:
                                paper_session.start(config)
                            except PaperRiskError as exc:
                                if str(exc) != "a paper session is already running":
                                    raise
                        elif action == "stop":
                            paper_session.stop()
                        else:
                            raise ValueError("unknown paper control action")
                        last_control_id = control_id
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    log.exception("invalid dashboard paper control")
            current = paper_session.store.load()
            if current is not None and current.status is SessionStatus.RUNNING:
                await paper_session.step(recorder.rest, builder.pricer, "KXBTC15M")

        async def write_snapshot() -> None:
            await apply_paper_control()
            dashboard_http.write_kind(store, "snapshot", clean(await builder.build()))
            paper_state = paper_store(settings.data_dir).load()
            if paper_state is not None:
                session_store = paper_store(settings.data_dir)
                dashboard_http.write_kind(
                    store,
                    "execution",
                    {
                        "state": paper_state.model_dump(mode="json"),
                        "history": session_store.history(),
                    },
                )

        recorder.on_minute = write_snapshot
    await recorder.run()


async def balance(settings: Settings) -> None:
    rest = KalshiRest(settings.kalshi_trade_url, signer=signer_from(settings))
    try:
        b = await rest.balance()
        print(f"{settings.env.value}: balance ${b.balance / 100:,.2f}")
    finally:
        await rest.aclose()


async def markets(settings: Settings) -> None:
    rest = KalshiRest(KALSHI_URLS["production"])
    try:
        for series in settings.record_series:
            ms = await rest.markets(series)
            closes = sorted({m.close_time for m in ms})
            print(f"{series}: {len(ms)} open markets, next close {closes[0] if closes else '-'}")
    finally:
        await rest.aclose()


async def backfill(settings: Settings, days: int) -> None:
    log = logging.getLogger("edgebook.backfill")
    # Spot needs extra history before the first market for the HAR warmup.
    added = await coinbase.backfill(settings.history_dir, days + calib.WARMUP_DAYS + 1)
    log.info("coinbase BTC-USD: +%d minutes", added)
    rest = KalshiRest(KALSHI_URLS["production"])
    try:
        for series in settings.calibration_series:
            n = await kalshi_history.backfill(rest, settings.history_dir, series, days)
            log.info("kalshi %s: +%d rows", series, n)
    finally:
        await rest.aclose()


# 15-minute markets only exist for 15 minutes, so they get shorter horizons.
SERIES_HORIZONS = {"KXBTC15M": (14, 10, 5, 2, 1)}


def horizons_for(series: str) -> tuple[int, ...]:
    return SERIES_HORIZONS.get(series, kalshi_history.HORIZONS_MIN)


async def calibrate(settings: Settings, days: int) -> None:
    ts, close = coinbase.load(settings.history_dir)
    cutoff = int(datetime.now(UTC).timestamp()) - days * 86_400
    rows = []
    for series in settings.calibration_series:
        markets = [
            m
            for m in kalshi_history.load_markets(settings.history_dir, series)
            if m["close_ts"] >= cutoff
        ]
        candles = kalshi_history.load_candles(settings.history_dir, series)
        rows += kalshi_history.horizon_rows(markets, candles, horizons_for(series))
    report = calib.run(
        ts,
        close,
        rows,
        progress=lambda i, n: logging.getLogger("edgebook.calibrate").info("%d/%d rows", i, n),
    )
    print(calib.render(report))
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    out = settings.reports_dir / f"calibration-{datetime.now(UTC):%Y-%m-%dT%H%M}.json"
    out.write_text(json.dumps(clean(report), indent=2))
    print(f"\nreport: {out}")
    if PUBLISH:
        await publish(settings, "calibration", report)


async def research(settings: Settings, days: int) -> None:
    ts, close = coinbase.load(settings.history_dir)
    cutoff = int(datetime.now(UTC).timestamp()) - days * 86_400
    report: dict[str, object] = {}
    for series in settings.calibration_series:
        markets = [
            m
            for m in kalshi_history.load_markets(settings.history_dir, series)
            if m["close_ts"] >= cutoff
        ]
        candles = kalshi_history.load_candles(settings.history_dir, series)
        fs = features.build(markets, candles, ts, close, horizons_for(series))
        results = strategies.run(fs)
        bias = strategies.price_bias(fs)
        print(strategies.render(series, fs, results))
        print(strategies.render_bias(bias))
        print()
        report[series] = {
            "rows": int(fs.y.size),
            "skipped": fs.skipped,
            "strategies": results,
            "price_bias": bias,
        }
    payload = {
        "generated": datetime.now(UTC),
        "days": days,
        "holdout_days": strategies.HOLDOUT_DAYS,
        "series": report,
    }
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    out = settings.reports_dir / f"research-{datetime.now(UTC):%Y-%m-%dT%H%M}.json"
    out.write_text(json.dumps(clean(payload), indent=2))
    print(f"report: {out}")
    if PUBLISH:
        await publish(settings, "research", payload)


def order_for(sig: Signal) -> CreateOrderV2Request:
    """1-contract IOC at the current quote: buy YES at the ask, or sell YES at the bid."""
    yes = sig.best_side == "yes"
    return CreateOrderV2Request(
        ticker=sig.ticker,
        client_order_id=str(uuid.uuid4()),
        side="bid" if yes else "ask",
        count="1.00",
        price=f"{sig.ask if yes else sig.bid:.4f}",
        time_in_force="immediate_or_cancel",
    )


def order_debit(sig: Signal) -> float:
    """Maximum dollars debited for the IOC order, including the taker fee."""
    price = Decimal(str(sig.ask if sig.best_side == "yes" else sig.bid))
    cost = price if sig.best_side == "yes" else Decimal("1") - price
    return float(cost + trade_fee(1, price, LiquidityRole.TAKER))


def session_spend(log_path: Path, env: str) -> float:
    """Read only prior reserved spend from the same environment's order log."""
    if not log_path.is_file():
        return 0.0
    total = 0.0
    for line in log_path.read_text().splitlines():
        try:
            record = json.loads(line)
            if record.get("env") == env:
                total += float(record.get("reserved_dollars", 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return total


async def trade_once(settings: Settings, args: argparse.Namespace) -> None:
    """Price the soonest market in a series and (optionally) take the best side once."""
    public = KalshiRest(KALSHI_URLS["production"])
    try:
        sigs = await signals(public, LivePricer(settings.history_dir), args.series)
    finally:
        await public.aclose()
    if not sigs:
        raise SystemExit(f"no priceable open market in {args.series}")
    sig = max(sigs, key=lambda s_: s_.best_edge)
    order = order_for(sig)
    cost = sig.ask if sig.best_side == "yes" else 1 - sig.bid
    print(
        f"{sig.ticker}  {sig.seconds_left / 60:.1f} min left  spot {sig.spot:,.2f}  "
        f"strike {sig.floor or sig.cap:,.2f}\n"
        f"  fair {sig.fair:.3f}  bid {sig.bid:.2f}  ask {sig.ask:.2f}  "
        f"edge YES {sig.edge_yes:+.3f}  NO {sig.edge_no:+.3f}  (after taker fee)\n"
        f"  best: buy {sig.best_side.upper()} 1 contract for ${cost:.2f}, "
        f"expected {sig.best_edge:+.3f} $/contract"
    )
    if not args.execute:
        print("dry run: add --execute to place it")
        return
    if sig.best_edge < args.min_edge:
        raise SystemExit(f"edge {sig.best_edge:+.3f} below --min-edge {args.min_edge}; not trading")
    if settings.env.value == "production" and not args.confirm_real_money:
        raise SystemExit("production order: pass --confirm-real-money to place a real trade")
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.reports_dir / "trades.jsonl"
    cap = settings.live_session_cap_dollars if args.session_cap is None else args.session_cap
    reserved = order_debit(sig)
    spent = session_spend(log_path, settings.env.value)
    if spent + reserved > cap:
        raise SystemExit(
            f"session cap ${cap:.2f}: ${spent:.2f} already reserved, "
            f"next order needs ${reserved:.2f}"
        )
    # Confirmation protects against accidental clicks; the viability gate protects
    # capital by requiring a separately measured, positive paper-trading result.
    assert_live_trading_allowed(settings)
    rest = KalshiRest(settings.kalshi_trade_url, signer=signer_from(settings))
    try:
        resp = await rest.create_order(order)
    finally:
        await rest.aclose()
    record: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "env": settings.env.value,
        "manual_one_off": True,
        "signal": dataclasses.asdict(sig),
        "order": order.model_dump(),
        "response": resp.model_dump(mode="json"),
    }
    record["session_cap_dollars"] = cap
    record["reserved_dollars"] = reserved
    record["session_reserved_dollars"] = spent + reserved
    with log_path.open("a") as f:
        f.write(json.dumps(clean(record)) + "\n")
    await publish(
        settings, "trades", [json.loads(line) for line in log_path.read_text().splitlines()]
    )
    print(
        f"order {resp.order_id}: filled {resp.fill_count} @ {resp.average_fill_price}, "
        f"fee {resp.average_fee_paid}, remaining {resp.remaining_count}"
    )


async def paper(settings: Settings, args: argparse.Namespace) -> None:
    """Start, stop, inspect, or step the local durable paper session."""
    session = PaperSession(paper_store(settings.data_dir))
    if args.paper_action == "start":
        budget = Decimal(str(args.paper_budget))
        state = session.start(
            SessionConfig(
                mode=ExecutionMode.PAPER,
                paper_starting_cash=budget,
                max_open_debit=Decimal(str(args.paper_max_open_debit)),
                max_order_debit=Decimal(str(args.paper_max_order_debit)),
                min_edge=Decimal(str(args.min_edge)),
                max_open_positions=args.paper_max_open_positions,
            )
        )
        print(f"paper session {state.session_id} started with ${state.paper_cash:.2f}")
        return
    if args.paper_action == "stop":
        state = session.stop()
        print(f"paper session {state.session_id} stopped")
        return
    if args.paper_action == "status":
        current = session.store.load()
        print("no paper session" if current is None else current.model_dump_json(indent=2))
        return
    rest = KalshiRest(KALSHI_URLS["production"])
    try:
        state = await session.step(rest, LivePricer(settings.history_dir), args.series)
    finally:
        await rest.aclose()
    print(
        f"paper {state.status}: cash ${state.paper_cash:.2f}, "
        f"open {len(state.positions)}, settled {len(state.settled)}, "
        f"realized P&L ${state.paper_realized_pnl:+.2f}"
    )


COMMANDS = {"record": record, "balance": balance, "markets": markets}
PUBLISH = False  # set by --publish
DAY_COMMANDS = {"backfill": backfill, "calibrate": calibrate, "research": research}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edgebook")
    parser.add_argument(
        "command", choices=sorted([*COMMANDS, *DAY_COMMANDS, "paper", "trade-once"])
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--series", default="KXBTC15M")
    parser.add_argument("--execute", action="store_true", help="actually place the order")
    parser.add_argument("--min-edge", type=float, default=0.0)
    parser.add_argument("--confirm-real-money", action="store_true")
    parser.add_argument(
        "--paper-action", choices=("start", "stop", "status", "step"), default="status"
    )
    parser.add_argument("--paper-budget", type=float, default=25.0)
    parser.add_argument("--paper-max-open-debit", type=float, default=5.0)
    parser.add_argument("--paper-max-order-debit", type=float, default=1.0)
    parser.add_argument("--paper-max-open-positions", type=int, default=1)
    parser.add_argument(
        "--session-cap", type=float, default=None, help="maximum cumulative order debit"
    )
    parser.add_argument("--publish", action="store_true", help="push the report to the dashboard")
    args = parser.parse_args(argv)
    global PUBLISH
    PUBLISH = args.publish
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        settings = Settings()
        if args.command == "trade-once":
            asyncio.run(trade_once(settings, args))
        elif args.command == "paper":
            asyncio.run(paper(settings, args))
        elif args.command in DAY_COMMANDS:
            asyncio.run(DAY_COMMANDS[args.command](settings, args.days))
        else:
            asyncio.run(COMMANDS[args.command](settings))
    except KeyboardInterrupt:
        return 130
    return 0
