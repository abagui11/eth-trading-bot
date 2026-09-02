"""End-to-end agent cycle: research -> charts -> analyze -> notify -> ledger -> paper."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import analyze
import audit
import bot_config
import charts
import critic
import display_summary
import ledger
import notify
import paper
import research
import twitter_post
import user_books
from macro.context import decision_macro_snapshot
from models import Suggestion
from patterns.htf_structure import detect_htf_zones
from patterns.key_levels import compute_key_levels
from patterns.market_context import MarketContext, build_market_context
from patterns.relative_strength import build_relative_strength_context

logger = logging.getLogger(__name__)


def run_cycle() -> list[tuple[Suggestion, list[str]]] | None:
    """Run one dual-asset cycle and return persisted product decisions."""
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger.info("Starting cycle %s", cycle_id)

    try:
        data_by_product: dict[str, dict[str, list[dict]]] = {}
        levels_by_product: dict[str, list] = {}
        zones_by_product: dict[str, list] = {}
        contexts_by_product: dict[str, MarketContext] = {}
        charts_by_product: dict[str, dict[str, str]] = {}

        for product_id in bot_config.TRADED_PRODUCTS:
            data = research.get_all_timeframes(product_id=product_id)
            daily_bars = research.get_daily_bars_for_levels(product_id=product_id)
            key_levels = compute_key_levels(daily_bars)
            htf_zones = detect_htf_zones(data["H4"], product_id=product_id)
            market_context = build_market_context(
                data["H4"],
                data["H1"],
                data["M5"],
                daily_bars=daily_bars,
                product_id=product_id,
            )
            marked_paths = charts.render_marked_charts(
                data,
                key_levels,
                htf_zones,
                cycle_id=cycle_id,
                market_context=market_context,
                product_id=product_id,
            )
            data_by_product[product_id] = data
            levels_by_product[product_id] = key_levels
            zones_by_product[product_id] = htf_zones
            contexts_by_product[product_id] = market_context
            charts_by_product[product_id] = marked_paths

        if bot_config.RELATIVE_STRENGTH_ENABLED:
            relative_strength = build_relative_strength_context()
            ratio_chart_path = charts.render_ratio_chart(relative_strength, cycle_id)
            relative_strength_text = relative_strength.summary_text
        else:
            ratio_chart_path = None
            relative_strength_text = (
                "## ETH/BTC relative strength (W1)\n"
                "Feature disabled; treat ETH and BTC with neutral weighting."
            )

        guide = analyze.load_trading_guide()
        suggestions = analyze.propose_trades_multi(
            charts_by_product,
            contexts_by_product,
            relative_strength_text,
            ratio_chart_path=ratio_chart_path,
            trading_guide=guide,
        )

        actionable = [s for s in suggestions if s.action != "no_trade"]
        if actionable:
            selected = actionable
        elif suggestions:
            selected = [suggestions[0]]
        else:
            first_product = bot_config.TRADED_PRODUCTS[0]
            selected = [
                Suggestion.no_trade(
                    "No valid dual-asset suggestion returned.",
                    product_id=first_product,
                )
            ]

        # Always persist a decision (trade or no_trade) per product so both
        # ETH and BTC marked charts stay available on the dashboard.
        selected_by_product = {s.product_id: s for s in selected}
        for product_id in bot_config.TRADED_PRODUCTS:
            if product_id not in selected_by_product:
                selected.append(
                    Suggestion.no_trade(
                        "No independent setup for this asset this cycle.",
                        product_id=product_id,
                    )
                )

        spots = research.get_spot_prices()
        results: list[tuple[Suggestion, list[str]]] = []
        for suggestion in selected:
            product_id = suggestion.product_id
            market_context = contexts_by_product[product_id]
            marked_paths = charts_by_product[product_id]
            product_cycle_id = (
                f"{cycle_id}_{bot_config.product_label(product_id).upper()}"
            )

            refine = critic.refine_suggestion(
                suggestion,
                market_context,
                marked_paths,
                guide,
            )
            suggestion = refine.suggestion
            suggestion.product_id = product_id
            llm_body = refine.llm_body
            context_block = critic.build_market_context_block(market_context.alerts)
            suggestion.rationale = critic.compose_rationale(llm_body, context_block)

            # Hard execution block: remaining critical findings on a trade → no_trade.
            critical_left = [
                f
                for f in refine.final_findings
                if f.severity == "critical" or f.code == "LLM_HALLUCINATION"
            ]
            if suggestion.action != "no_trade" and critical_left:
                codes = sorted({f.code for f in critical_left})
                logger.warning(
                    "Hard audit block for %s: findings=%s",
                    product_id,
                    codes,
                )
                llm_body = critic.sanitize_rationale(
                    market_context, downgrade_reason=codes
                )
                suggestion = Suggestion.no_trade(llm_body, product_id=product_id)
                suggestion.decision_charts = ["H4"]
                suggestion.rationale = critic.compose_rationale(
                    llm_body, context_block
                )

            output_paths = charts.build_trade_broadcast_charts(
                suggestion,
                data_by_product[product_id],
                levels_by_product[product_id],
                zones_by_product[product_id],
                product_cycle_id,
                market_context=market_context,
            )
            chart_for_ledger = ",".join(output_paths)
            price = spots.get(product_id, market_context.spot)
            setup_tags = (
                ",".join(market_context.setup_tags)
                if market_context.setup_tags
                else None
            )
            macro_snap = decision_macro_snapshot()
            row_id = ledger.append(
                suggestion,
                product_cycle_id,
                price,
                chart_for_ledger,
                setup_tags=setup_tags,
                executed=suggestion.action != "no_trade",
                macro_json=macro_snap,
            )
            ledger.require_cycle_recorded(product_cycle_id)
            # House/agent book only — user books open on Accept.
            paper.update(
                suggestion,
                spots.get("ETH-USD", price),
                cycle_id=product_cycle_id,
                spots=spots,
            )
            # A fresh read of the same chart that finds nothing retires any
            # plan still waiting on it: silence would leave a resting order
            # Eva would no longer write.
            if suggestion.action == "no_trade":
                import live_pending

                live_pending.cancel(product_id)
            # Live mirror (EXECUTION_MODE=shadow|live). Never blocks the
            # paper path — failures log + halt inside execute.
            offer_id = None
            card_summary = None
            if suggestion.action != "no_trade":
                import execute
                import vault

                try:
                    card_summary = display_summary.generate_display_summary(
                        suggestion
                    )
                except Exception:
                    logger.exception(
                        "Display summary generation failed for %s", product_cycle_id
                    )
                    card_summary = None
                hq_title = display_summary.friendly_title(suggestion)
                if not display_summary.is_watchdog_suggestion(suggestion):
                    hq_title = f"High Quality · {hq_title}"
                import live_pending

                # Eva's entry is a pullback into an M5 block, so the market is
                # usually not there yet. Waiting for it beats buying the mark:
                # the stop does not move, so every point of chase both widens
                # the risk and shortens the run to the first target.
                spot_now = spots.get(product_id, price)
                waits = bot_config.LIVE_PENDING_ENTRIES_ENABLED and not (
                    live_pending.is_fillable(
                        live_pending.side_of(suggestion.action),
                        suggestion.entry,
                        spot_now,
                    )
                )
                # A waiting plan fills at its own entry, so that is the price
                # the clip has to be sized against — sizing it off today's mark
                # would risk the wrong amount by the width of the gap.
                vault.take(
                    suggestion,
                    cycle_id=product_cycle_id,
                    spot=float(suggestion.entry) if waits else spot_now,
                    title=hq_title,
                    blurb=card_summary,
                )
                if waits:
                    live_pending.record(suggestion, cycle_id=product_cycle_id)
                else:
                    execute.maybe_execute_live(
                        suggestion,
                        spot_now,
                        cycle_id=product_cycle_id,
                        source="hq",
                    )
                house_pos_id = user_books.find_house_position_id_for_cycle(
                    product_cycle_id
                )
                offer = user_books.create_trade_offer(
                    cycle_id=product_cycle_id,
                    suggestion=suggestion,
                    chart_paths=output_paths,
                    house_position_id=house_pos_id,
                    display_summary=card_summary,
                )
                if offer:
                    offer_id = offer["offer_id"]
            user_books.expire_pending_decisions()
            user_books.check_user_sl_tp(spots=spots)
            pnl_footer = paper.format_pnl_footer(spots=spots)
            broadcast_sent = (
                suggestion.action != "no_trade"
                or not bot_config.BROADCAST_ONLY_TRADES
            )

            try:
                audit.save_snapshot(
                    product_cycle_id,
                    market_context,
                    suggestion,
                    marked_paths,
                    llm_rationale=llm_body,
                    signals_block=context_block,
                )
                verdict = critic.audit_hourly_cycle(
                    product_cycle_id,
                    suggestion,
                    market_context,
                    marked_paths,
                    llm_rationale=llm_body,
                    run_llm=False,
                    sanitized=refine.sanitized,
                    downgraded=refine.downgraded,
                    passes_used=refine.passes_used,
                    sanitize_reasons=refine.sanitize_reasons,
                )
                notify.send_hourly_monitor_report(
                    verdict, broadcast_sent=broadcast_sent
                )
            except Exception:
                logger.exception(
                    "Monitor audit failed for cycle %s", product_cycle_id
                )

            try:
                if broadcast_sent:
                    notify.broadcast(
                        suggestion,
                        output_paths,
                        pnl_footer=pnl_footer,
                        offer_id=offer_id,
                        display_summary_text=card_summary,
                        # False = public subscribers get HQ cards with the
                        # High Quality label; True gates to the internal
                        # ops allowlist.
                        internal_only=bot_config.HQ_IDEAS_INTERNAL_ONLY,
                    )
                    # Announcement-only mirror on X (no Accept/Reject there).
                    twitter_post.announce_hq(
                        suggestion,
                        summary=card_summary,
                        chart_paths=output_paths,
                    )
                else:
                    logger.info(
                        "Skipping subscriber broadcast — %s for cycle %s",
                        suggestion.action,
                        product_cycle_id,
                    )
            except Exception:
                logger.exception("Broadcast failed for cycle %s", product_cycle_id)

            try:
                notify.process_missed_connections(spots=spots)
            except Exception:
                logger.exception("Missed-connection sweep failed")

            logger.info(
                "Cycle %s complete: product=%s action=%s ledger_id=%s charts=%s "
                "sanitized=%s downgraded=%s passes=%s offer=%s",
                product_cycle_id,
                product_id,
                suggestion.action,
                row_id,
                output_paths,
                refine.sanitized,
                refine.downgraded,
                refine.passes_used,
                offer_id,
            )
            results.append((suggestion, output_paths))

        notify.maybe_send_launch_notice()
        return results
    except Exception:
        logger.exception("Cycle %s failed", cycle_id)
        return None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_cycle()
