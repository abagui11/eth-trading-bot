"""End-to-end agent cycle: research -> charts -> analyze -> notify -> ledger -> paper."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import analyze
import audit
import bot_config
import charts
import critic
import ledger
import notify
import paper
import research
from models import Suggestion
from patterns.htf_structure import detect_htf_zones
from patterns.key_levels import compute_key_levels
from patterns.market_context import MarketContext, build_market_context

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
            data,
            key_levels,
            htf_zones,
            cycle_id=cycle_id,
            market_context=market_context,
        )

        guide = analyze.load_trading_guide()
        suggestion = analyze.propose_trade(
            marked_paths,
            trading_guide=guide,
            market_context=market_context,
        )

        refine = critic.refine_suggestion(
            suggestion,
            market_context,
            marked_paths,
            guide,
        )
        suggestion = refine.suggestion
        llm_body = refine.llm_body
        signals_block = critic.build_signals_block(market_context.alerts)
        suggestion.rationale = critic.compose_rationale(llm_body, signals_block)

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
        ledger.require_cycle_recorded(cycle_id)
        paper.update(suggestion, price, cycle_id=cycle_id)
        pnl_footer = paper.format_pnl_footer(price)

        broadcast_sent = (
            suggestion.action != "no_trade"
            or not bot_config.BROADCAST_ONLY_TRADES
        )

        try:
            audit.save_snapshot(
                cycle_id,
                market_context,
                suggestion,
                marked_paths,
                llm_rationale=llm_body,
                signals_block=signals_block,
            )
            verdict = critic.audit_hourly_cycle(
                cycle_id,
                suggestion,
                market_context,
                marked_paths,
                llm_rationale=llm_body,
                run_llm=False,
                sanitized=refine.sanitized,
                downgraded=refine.downgraded,
                passes_used=refine.passes_used,
            )
            notify.send_hourly_monitor_report(verdict, broadcast_sent=broadcast_sent)
        except Exception:
            logger.exception("Monitor audit failed for cycle %s", cycle_id)

        # TODO: validate.py — Layer 2 risk caps, R/R enforcement, size recompute
        try:
            if broadcast_sent:
                notify.broadcast(suggestion, output_paths, pnl_footer=pnl_footer)
            else:
                logger.info(
                    "Skipping subscriber broadcast — %s for cycle %s",
                    suggestion.action,
                    cycle_id,
                )
        except Exception:
            logger.exception("Broadcast failed for cycle %s", cycle_id)

        # TODO: execute.py — EXECUTION_MODE=shadow|live order path
        logger.info(
            "Cycle %s complete: action=%s ledger_id=%s charts=%s sanitized=%s downgraded=%s passes=%s",
            cycle_id,
            suggestion.action,
            row_id,
            output_paths,
            refine.sanitized,
            refine.downgraded,
            refine.passes_used,
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
