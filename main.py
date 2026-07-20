"""Entry point: Telegram bot polling + hourly agent cycle."""

from __future__ import annotations

import asyncio
import logging
import sys

from bot import build_application
from agent import run_cycle
from watchdog import run_watchdog
import bot_config
from macro.ingest import poll_feeds
from zmove import run_zmove_scan

logger = logging.getLogger(__name__)

HOURLY_INTERVAL_SEC = 3600
FIRST_RUN_DELAY_SEC = 10


async def watchdog_job(context) -> None:
    """Run the programmatic entry scanner in a thread pool."""
    if not bot_config.WATCHDOG_ENABLED:
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, run_watchdog)
    except Exception:
        logger.exception("Watchdog job failed")


async def macro_feed_job(context) -> None:
    """Poll RSS feeds for macro headlines."""
    if not bot_config.MACRO_CONTEXT_ENABLED:
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, poll_feeds)
    except Exception:
        logger.exception("Macro feed job failed")


async def zmove_job(context) -> None:
    """Scan ETH H1 price/volume z-scores and broadcast spikes."""
    if not bot_config.ZMOVE_ENABLED:
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, run_zmove_scan)
    except Exception:
        logger.exception("Z-Move job failed")


async def hourly_job(context) -> None:
    """Run the sync agent cycle in a thread pool."""
    logger.info("Hourly job starting")
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, run_cycle)
    except Exception:
        logger.exception("Hourly job failed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    # Python 3.10+ on Windows: ensure main thread has an event loop for PTB.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = build_application()
    if app.job_queue is None:
        raise RuntimeError("JobQueue unavailable — install python-telegram-bot[job-queue]")

    app.job_queue.run_repeating(
        hourly_job,
        interval=HOURLY_INTERVAL_SEC,
        first=FIRST_RUN_DELAY_SEC,
        name="hourly_cycle",
    )

    if bot_config.WATCHDOG_ENABLED:
        interval = max(60, min(bot_config.WATCHDOG_INTERVAL_SEC, 300))
        app.job_queue.run_repeating(
            watchdog_job,
            interval=interval,
            first=30,
            name="watchdog_scan",
        )
        logger.info("Watchdog enabled — scanning every %ss", interval)

    if bot_config.MACRO_CONTEXT_ENABLED:
        macro_interval = max(60, bot_config.MACRO_POLL_INTERVAL_SEC)
        app.job_queue.run_repeating(
            macro_feed_job,
            interval=macro_interval,
            first=60,
            name="macro_feed_poll",
        )
        logger.info("Macro feed poll enabled — every %ss", macro_interval)

    if bot_config.ZMOVE_ENABLED:
        zmove_interval = max(60, bot_config.ZMOVE_INTERVAL_SEC)
        app.job_queue.run_repeating(
            zmove_job,
            interval=zmove_interval,
            first=90,
            name="zmove_scan",
        )
        logger.info("Z-Move scan enabled — every %ss", zmove_interval)

    logger.info(
        "Starting ETH trading agent (polling + hourly cycle every %ss, first in %ss)",
        HOURLY_INTERVAL_SEC,
        FIRST_RUN_DELAY_SEC,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
