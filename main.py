"""Entry point: Telegram bot polling + hourly agent cycle."""

from __future__ import annotations

import asyncio
import logging
import sys

from bot import build_application
from agent import run_cycle

logger = logging.getLogger(__name__)

HOURLY_INTERVAL_SEC = 3600
FIRST_RUN_DELAY_SEC = 10


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

    logger.info(
        "Starting ETH trading agent (polling + hourly cycle every %ss, first in %ss)",
        HOURLY_INTERVAL_SEC,
        FIRST_RUN_DELAY_SEC,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
