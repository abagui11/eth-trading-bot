"""CLI to backfill historical OHLC into ohlc.db."""

from __future__ import annotations

import argparse
import json
import logging
import sys

import ohlc_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill ETH-USD OHLC into ohlc.db")
    parser.add_argument(
        "--years",
        type=int,
        default=4,
        help="Years of history to fetch (default: 4)",
    )
    parser.add_argument(
        "--hourly",
        action="store_true",
        help="Backfill H1 candles (needed for H12 SFP research)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Backfill both daily and hourly history",
    )
    args = parser.parse_args()

    results: dict = {}
    if args.all or not args.hourly:
        logger.info("Backfilling %s years of daily candles...", args.years)
        results["daily"] = ohlc_cache.backfill_daily(years=args.years)
        logger.info(
            "Daily: %s bars (%s to %s)",
            results["daily"]["count"],
            results["daily"]["min_ts"],
            results["daily"]["max_ts"],
        )

    if args.hourly or args.all:
        logger.info("Backfilling %s years of H1 candles (may take a few minutes)...", args.years)
        results["hourly"] = ohlc_cache.backfill_hourly(years=args.years)
        logger.info(
            "Hourly: %s bars (%s to %s)",
            results["hourly"]["count"],
            results["hourly"]["min_ts"],
            results["hourly"]["max_ts"],
        )

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
