"""End-to-end agent cycle: research -> charts -> analyze -> notify -> ledger -> paper."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import analyze
import charts
import ledger
import notify
import paper
import research
from models import Suggestion
from patterns.market_context import build_market_context

logger = logging.getLogger(__name__)


def run_cycle() -> tuple[Suggestion, str] | None:
    """Run one full cycle. Returns (suggestion, annotated_path) on success."""
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger.info("Starting cycle %s", cycle_id)

    try:
        data = research.get_all_timeframes()
        market_context = build_market_context(data["H12"], data["H4"], data["H1"])
        chart_paths = charts.render_charts(data, cycle_id=cycle_id)

        guide = analyze.load_trading_guide()
        suggestion = analyze.propose_trade(
            chart_paths,
            trading_guide=guide,
            market_context=market_context,
        )

        if market_context.alerts:
            alert_block = "Signals: " + " | ".join(market_context.alerts)
            suggestion.rationale = f"{alert_block}\n\n{suggestion.rationale}".strip()

        annotated = charts.annotate_chart(
            chart_paths["H1"],
            suggestion,
            cycle_id,
            h1_bars=data["H1"],
            market_context=market_context,
        )

        price = research.get_spot_price()
        setup_tags = ",".join(market_context.setup_tags) if market_context.setup_tags else None
        row_id = ledger.append(
            suggestion,
            cycle_id,
            price,
            annotated,
            setup_tags=setup_tags,
        )
        paper.update(suggestion, price, cycle_id=cycle_id)
        pnl_footer = paper.format_pnl_footer(price)

        # TODO: validate.py — Layer 2 risk caps, R/R enforcement, size recompute
        try:
            notify.broadcast(suggestion, annotated, pnl_footer=pnl_footer)
        except Exception:
            logger.exception("Broadcast failed for cycle %s", cycle_id)

        # TODO: execute.py — EXECUTION_MODE=shadow|live order path
        logger.info(
            "Cycle %s complete: action=%s ledger_id=%s chart=%s",
            cycle_id,
            suggestion.action,
            row_id,
            annotated,
        )
        return suggestion, annotated
    except Exception:
        logger.exception("Cycle %s failed", cycle_id)
        return None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_cycle()
