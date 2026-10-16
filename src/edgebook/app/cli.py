"""Command line entrypoints.

python -m edgebook record    # stream prod market data to Parquet (needs a prod key)
python -m edgebook balance   # signed call to verify a key against EDGEBOOK_ENV
python -m edgebook markets   # list open markets per series (public, no key)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from edgebook.config import KALSHI_URLS, Settings
from edgebook.recorder.run import Recorder
from edgebook.recorder.sink import ParquetSink
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


COMMANDS = {"record": record, "balance": balance, "markets": markets}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edgebook")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        asyncio.run(COMMANDS[args.command](Settings()))
    except KeyboardInterrupt:
        return 130
    return 0
