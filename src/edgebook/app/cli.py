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
from collections.abc import Sequence
from datetime import UTC, datetime

from edgebook.config import KALSHI_URLS, Settings
from edgebook.history import coinbase
from edgebook.history import kalshi as kalshi_history
from edgebook.recorder.run import Recorder
from edgebook.recorder.sink import ParquetSink
from edgebook.research import calibrate as calib
from edgebook.venues.kalshi.auth import KalshiSigner
from edgebook.venues.kalshi.rest import KalshiRest
from edgebook.venues.kalshi.ws import KALSHI_WS_URLS, KalshiWebSocket


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


async def calibrate(settings: Settings, days: int) -> None:
    ts, close = coinbase.load(settings.history_dir)
    cutoff = int(datetime.now(UTC).timestamp()) - days * 86_400
    rows = [
        r
        for series in settings.calibration_series
        for r in kalshi_history.load(settings.history_dir, series)
        if r["close_ts"] >= cutoff
    ]
    report = calib.run(
        ts,
        close,
        rows,
        progress=lambda i, n: logging.getLogger("edgebook.calibrate").info("%d/%d rows", i, n),
    )
    print(calib.render(report))
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    out = settings.reports_dir / f"calibration-{datetime.now(UTC):%Y-%m-%dT%H%M}.json"
    out.write_text(json.dumps(dataclasses.asdict(report), indent=2, default=str))
    print(f"\nreport: {out}")


COMMANDS = {"record": record, "balance": balance, "markets": markets}
DAY_COMMANDS = {"backfill": backfill, "calibrate": calibrate}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edgebook")
    parser.add_argument("command", choices=sorted([*COMMANDS, *DAY_COMMANDS]))
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        settings = Settings()
        if args.command in DAY_COMMANDS:
            asyncio.run(DAY_COMMANDS[args.command](settings, args.days))
        else:
            asyncio.run(COMMANDS[args.command](settings))
    except KeyboardInterrupt:
        return 130
    return 0
