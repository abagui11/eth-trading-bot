"""Entry point: Telegram bot polling + hourly agent cycle."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from bot import build_application
from agent import run_cycle
from watchdog import run_watchdog
import bot_config
from macro.ingest import poll_feeds
from zmove import run_zmove_scan

logger = logging.getLogger(__name__)

HOURLY_INTERVAL_SEC = 3600
FIRST_RUN_DELAY_SEC = 10


def seconds_until_next_hour(now: float | None = None) -> float:
    """Delay until the next wall-clock top of hour (minimum 10s guard)."""
    ts = now if now is not None else time.time()
    remainder = ts % 3600
    delay = 3600 - remainder
    return max(delay, 10.0)


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


async def stance_job(context) -> None:
    """Persist the hourly BTC/ETH multi-timeframe stance batch."""
    if not bot_config.INTELLIGENCE_ENABLED:
        return
    from intelligence.stance import run_stance_cycle

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, run_stance_cycle)
    except Exception:
        logger.exception("Stance job failed")


async def funding_job(context) -> None:
    """Refresh perp funding prints and recompute funding regimes."""
    if not bot_config.FUNDING_ENABLED:
        return
    from intelligence.funding import run_funding_scan

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, run_funding_scan)
    except Exception:
        logger.exception("Funding job failed")


async def long_thesis_job(context) -> None:
    """Refresh the BTC 4-year-cycle long thesis (daily)."""
    if not bot_config.LONG_THESIS_ENABLED:
        return
    from intelligence.cycle_thesis import run_long_thesis_refresh

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, run_long_thesis_refresh)
    except Exception:
        logger.exception("Long thesis job failed")


async def hourly_job(context) -> None:
    """Run the hourly stance batch, then the sync agent cycle, in a thread pool."""
    logger.info("Hourly job starting")
    await stance_job(context)
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

    # Wall-clock alignment: first hourly run fires at the next top of hour, and
    # a bootstrap run fires shortly after start so restarts don't leave a gap.
    first_hourly = seconds_until_next_hour()
    app.job_queue.run_repeating(
        hourly_job,
        interval=HOURLY_INTERVAL_SEC,
        first=first_hourly,
        name="hourly_cycle",
    )
    app.job_queue.run_once(hourly_job, when=FIRST_RUN_DELAY_SEC, name="bootstrap_cycle")

    if bot_config.FUNDING_ENABLED:
        app.job_queue.run_repeating(
            funding_job,
            interval=max(300, bot_config.FUNDING_INTERVAL_SEC),
            first=20,
            name="funding_scan",
        )
        logger.info("Funding regime scan enabled — every %ss", bot_config.FUNDING_INTERVAL_SEC)

    if bot_config.LONG_THESIS_ENABLED:
        app.job_queue.run_repeating(
            long_thesis_job,
            interval=max(3600, bot_config.LONG_THESIS_INTERVAL_SEC),
            first=120,
            name="long_thesis_refresh",
        )
        logger.info("Long thesis refresh enabled — every %ss", bot_config.LONG_THESIS_INTERVAL_SEC)

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
        "Starting ETH trading agent (polling + hourly cycle on the hour, next in %.0fs)",
        first_hourly,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
