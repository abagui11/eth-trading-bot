"""Deprecated: use main.py (bot polling + hourly job). Kept for one-shot scheduler testing."""

from __future__ import annotations

import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from agent import run_cycle

logger = logging.getLogger(__name__)


def _run_cycle_safe() -> None:
    try:
        run_cycle()
    except Exception:
        logger.exception("Scheduled cycle failed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    logger.warning("scheduler.py is deprecated — use main.py for production")
    _run_cycle_safe()

    scheduler = BlockingScheduler()
    scheduler.add_job(
        _run_cycle_safe,
        IntervalTrigger(hours=1),
        id="hourly_cycle",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
