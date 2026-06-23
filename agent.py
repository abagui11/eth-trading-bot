"""End-to-end agent cycle: research -> charts -> analyze -> notify -> ledger."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import analyze
import charts
import ledger
import notify
import research

logger = logging.getLogger(__name__)


def run_cycle() -> None:
    """Run one full broadcast cycle."""
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger.info("Starting cycle %s", cycle_id)

    try:
        data = research.get_all_timeframes()
        chart_paths = charts.render_charts(data, cycle_id=cycle_id)

        rules = analyze.load_rules()
        suggestion = analyze.propose_trade(chart_paths, rules=rules)

        annotated = charts.annotate_chart(
            chart_paths["H1"],
            suggestion,
            cycle_id,
            h1_bars=data["H1"],
        )

        # TODO: validate.py — Layer 2 risk caps, R/R enforcement, size recompute
        try:
            notify.broadcast(suggestion, annotated)
        except Exception:
            logger.exception("Broadcast failed for cycle %s", cycle_id)

        price = research.get_spot_price()
        row_id = ledger.append(suggestion, cycle_id, price, annotated)

        # TODO: execute.py — EXECUTION_MODE=shadow|live order path
        logger.info(
            "Cycle %s complete: action=%s ledger_id=%s chart=%s",
            cycle_id,
            suggestion.action,
            row_id,
            annotated,
        )
    except Exception:
        logger.exception("Cycle %s failed", cycle_id)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_cycle()
