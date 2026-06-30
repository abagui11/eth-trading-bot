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
from patterns.htf_structure import detect_htf_zones
from patterns.key_levels import compute_key_levels
from patterns.market_context import build_market_context

logger = logging.getLogger(__name__)


def run_cycle() -> tuple[Suggestion, list[str]] | None:
    """Run one full cycle. Returns (suggestion, output_chart_paths) on success."""
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger.info("Starting cycle %s", cycle_id)

    try:
        data = research.get_all_timeframes()
        daily_bars = research.get_daily_bars_for_levels()
        key_levels = compute_key_levels(daily_bars)
        htf_zones = detect_htf_zones(data["H12"])
        market_context = build_market_context(
            data["H12"], data["H4"], data["H1"], daily_bars=daily_bars
        )
        marked_paths = charts.render_marked_charts(
            data, key_levels, htf_zones, cycle_id=cycle_id
        )

        guide = analyze.load_trading_guide()
        suggestion = analyze.propose_trade(
            marked_paths,
            trading_guide=guide,
            market_context=market_context,
        )

        if market_context.alerts:
            alert_block = "Signals: " + " | ".join(market_context.alerts)
            suggestion.rationale = f"{alert_block}\n\n{suggestion.rationale}".strip()

        output_paths = charts.build_output_charts(
            suggestion,
            data,
            key_levels,
            htf_zones,
            cycle_id,
            market_context=market_context,
        )
        chart_for_ledger = ",".join(output_paths)

        price = research.get_spot_price()
        setup_tags = ",".join(market_context.setup_tags) if market_context.setup_tags else None
        row_id = ledger.append(
            suggestion,
            cycle_id,
            price,
            chart_for_ledger,
            setup_tags=setup_tags,
        )
        paper.update(suggestion, price, cycle_id=cycle_id)
        pnl_footer = paper.format_pnl_footer(price)

        # TODO: validate.py — Layer 2 risk caps, R/R enforcement, size recompute
        try:
            notify.broadcast(suggestion, output_paths, pnl_footer=pnl_footer)
        except Exception:
            logger.exception("Broadcast failed for cycle %s", cycle_id)

        # TODO: execute.py — EXECUTION_MODE=shadow|live order path
        logger.info(
            "Cycle %s complete: action=%s ledger_id=%s charts=%s",
            cycle_id,
            suggestion.action,
            row_id,
            output_paths,
        )
        return suggestion, output_paths
    except Exception:
        logger.exception("Cycle %s failed", cycle_id)
        return None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_cycle()
