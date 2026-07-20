"""CLI to backfill historical OHLC into ohlc.db."""

from __future__ import annotations

import argparse
import json
import logging
import sys

import bot_config
import ohlc_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _resolve_products(product_arg: str) -> list[str]:
    normalized = product_arg.strip().upper()
    if normalized == "ALL":
        return list(bot_config.TRADED_PRODUCTS)
    if normalized in ("ETH", "ETH-USD"):
        return ["ETH-USD"]
    if normalized in ("BTC", "BTC-USD"):
        return ["BTC-USD"]
    if normalized in bot_config.TRADED_PRODUCTS:
        return [normalized]
    raise SystemExit(
        f"Unknown product: {product_arg}. Use ETH-USD, BTC-USD, or all."
    )


def _backfill_product(product_id: str, years: int, *, hourly: bool, do_all: bool) -> dict:
    results: dict = {"product_id": product_id}
    if do_all or not hourly:
        logger.info("Backfilling %s years of daily candles for %s...", years, product_id)
        results["daily"] = ohlc_cache.backfill_daily(years=years, product_id=product_id)
        logger.info(
            "Daily %s: %s bars (%s to %s)",
            product_id,
            results["daily"]["count"],
            results["daily"]["min_ts"],
            results["daily"]["max_ts"],
        )
        try:
            from patterns import sfp_index

            rebuilt = sfp_index.rebuild_sfp_index(product_id, "D1", years)
            results["sfp_index_d1"] = rebuilt
            rebuilt_w1 = sfp_index.rebuild_sfp_index(product_id, "W1", years)
            results["sfp_index_w1"] = rebuilt_w1
        except Exception:
            logger.exception("SFP index rebuild after daily backfill failed for %s", product_id)

    if hourly or do_all:
        logger.info(
            "Backfilling %s years of H1 candles for %s (may take a few minutes)...",
            years,
            product_id,
        )
        results["hourly"] = ohlc_cache.backfill_hourly(years=years, product_id=product_id)
        logger.info(
            "Hourly %s: %s bars (%s to %s)",
            product_id,
            results["hourly"]["count"],
            results["hourly"]["min_ts"],
            results["hourly"]["max_ts"],
        )
        try:
            from patterns import sfp_index

            rebuilt = sfp_index.rebuild_sfp_index(product_id, "H12", years)
            results["sfp_index_h12"] = rebuilt
        except Exception:
            logger.exception("SFP index rebuild after hourly backfill failed for %s", product_id)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill ETH-USD / BTC-USD OHLC into ohlc.db"
    )
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
    parser.add_argument(
        "--product",
        default="ETH-USD",
        help="ETH-USD, BTC-USD, or all (default: ETH-USD)",
    )
    args = parser.parse_args()

    products = _resolve_products(args.product)
    out: dict = {"products": {}}
    for product_id in products:
        out["products"][product_id] = _backfill_product(
            product_id,
            args.years,
            hourly=args.hourly,
            do_all=args.all,
        )

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
