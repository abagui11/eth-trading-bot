"""Sub-hourly programmatic entry scanner.

Runs between hourly vision cycles. When deterministic triggers fire (H1 OB fib,
bearish retest rejection, H1 SFP on close), builds and validates a trade,
renders structure/entry charts, then records and broadcasts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import analyze
import bot_config
import charts
import config
import critic
import ledger
import notify
import paper
import research
import validate
from models import Suggestion
from patterns.htf_structure import HTFZone, detect_htf_zones
from patterns.key_levels import compute_key_levels
from patterns.market_context import MarketContext, build_market_context
from patterns.order_block import (
    OrderBlock,
    fib_level,
    fib_zone_bounds,
    price_in_ob,
    zones_overlap,
)
from patterns.signal_state import get_state, set_state
from patterns.swing import Pivot, find_pivots
from patterns.sfp import SFPEvent

logger = logging.getLogger(__name__)

WATCHDOG_STATE_KEY = "watchdog_last_fire"
Direction = Literal["bullish", "bearish"]
SFP_TP_PCT = 0.02
SL_BUFFER_PCT = 0.0025


@dataclass(frozen=True)
class WatchdogTrigger:
    name: str
    direction: Direction
    ob: OrderBlock
    reason: str
    priority: int
    use_sfp_tp: bool = False
    sfp_event: SFPEvent | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cycle_id() -> str:
    return datetime.now(timezone.utc).strftime("WD%Y%m%dT%H%M%SZ")


def _trigger_key(trigger: WatchdogTrigger) -> str:
    sfp_ts = trigger.sfp_event.ts if trigger.sfp_event else ""
    return f"{trigger.name}:{trigger.ob.displacement_ts}:{sfp_ts}"


def _htf_allows_long(ctx: MarketContext) -> bool:
    if "htf_zone_conflict" in ctx.setup_tags:
        return False
    snap = ctx.zone_snapshot
    if snap is None:
        return False
    if snap.primary_bearish and not snap.primary_bullish:
        return False
    return snap.primary_bullish is not None


def _htf_allows_short(ctx: MarketContext) -> bool:
    if "htf_zone_conflict" in ctx.setup_tags:
        return False
    snap = ctx.zone_snapshot
    if snap is None:
        return False
    if snap.primary_bullish and not snap.primary_bearish:
        return False
    return snap.primary_bearish is not None or "short_trigger_retest" in ctx.setup_tags


def _obs_in_fib(ctx: MarketContext, direction: Direction) -> list[OrderBlock]:
    matches = [
        ob
        for ob in ctx.order_blocks
        if ob.direction == direction and price_in_ob(ctx.spot, ob)
    ]
    return sorted(matches, key=lambda ob: ob.displacement_ts, reverse=True)


def _latest_h1_bar_ts(h1_bars: list[dict]) -> str | None:
    if not h1_bars:
        return None
    return str(h1_bars[-1]["ts"])


def _sfp_on_latest_bar(event: SFPEvent, h1_bars: list[dict]) -> bool:
    latest = _latest_h1_bar_ts(h1_bars)
    if latest is None:
        return False
    return event.ts == latest


def _fresh_h1_sfp(ctx: MarketContext, h1_bars: list[dict]) -> SFPEvent | None:
    for event in reversed(ctx.h1_sfps):
        if event.outcome_a not in ("reversal", "pending"):
            continue
        if _sfp_on_latest_bar(event, h1_bars):
            return event
    return None


def evaluate_triggers(ctx: MarketContext, h1_bars: list[dict]) -> list[WatchdogTrigger]:
    """Return actionable triggers sorted by priority (highest first)."""
    triggers: list[WatchdogTrigger] = []

    if "short_trigger_retest" in ctx.setup_tags:
        for ob in _obs_in_fib(ctx, "bearish"):
            triggers.append(
                WatchdogTrigger(
                    name="short_trigger_retest",
                    direction="bearish",
                    ob=ob,
                    reason=(
                        "Bearish HTF retest rejection + H1 OB fib zone — "
                        "programmatic short trigger"
                    ),
                    priority=100,
                )
            )
            break

    sfp = _fresh_h1_sfp(ctx, h1_bars)
    if sfp is not None:
        direction: Direction = sfp.direction
        for ob in _obs_in_fib(ctx, direction):
            triggers.append(
                WatchdogTrigger(
                    name="h1_sfp_close",
                    direction=direction,
                    ob=ob,
                    reason=(
                        f"H1 {direction} SFP confirmed on latest bar close @ "
                        f"{sfp.swept_level:,.2f} with price in H1 OB fib zone"
                    ),
                    priority=90,
                    use_sfp_tp=True,
                    sfp_event=sfp,
                )
            )
            break

    if "h1_ob_bullish_in_fib" in ctx.setup_tags and _htf_allows_long(ctx):
        for ob in _obs_in_fib(ctx, "bullish"):
            triggers.append(
                WatchdogTrigger(
                    name="h1_ob_fib_long",
                    direction="bullish",
                    ob=ob,
                    reason=(
                        "Price in bullish H1 OB fib 0.618–0.786 with aligned HTF structure"
                    ),
                    priority=70,
                )
            )
            break

    if "h1_ob_bearish_in_fib" in ctx.setup_tags and _htf_allows_short(ctx):
        for ob in _obs_in_fib(ctx, "bearish"):
            triggers.append(
                WatchdogTrigger(
                    name="h1_ob_fib_short",
                    direction="bearish",
                    ob=ob,
                    reason=(
                        "Price in bearish H1 OB fib 0.618–0.786 with aligned HTF structure"
                    ),
                    priority=70,
                )
            )
            break

    triggers.sort(key=lambda t: t.priority, reverse=True)
    return triggers


def _swing_levels(h12_bars: list[dict]) -> list[Pivot]:
    if len(h12_bars) < 10:
        return []
    df = research.to_dataframe(h12_bars)
    return find_pivots(df)


def _stop_and_targets(
    *,
    entry: float,
    direction: Direction,
    h12_bars: list[dict],
    use_sfp_tp: bool,
) -> tuple[float, list[float]]:
    if use_sfp_tp:
        if direction == "bullish":
            tp = round(entry * (1 + SFP_TP_PCT), 2)
            stop = round(entry * (1 - max(SFP_TP_PCT, SL_BUFFER_PCT)), 2)
        else:
            tp = round(entry * (1 - SFP_TP_PCT), 2)
            stop = round(entry * (1 + max(SFP_TP_PCT, SL_BUFFER_PCT)), 2)
        return stop, [tp]

    pivots = _swing_levels(h12_bars)
    if direction == "bullish":
        lows = [p for p in pivots if p.kind == "low" and p.price < entry]
        if lows:
            swing = max(lows, key=lambda p: p.price)
            stop = round(swing.price * (1 - SL_BUFFER_PCT), 2)
        else:
            stop = round(entry * (1 - SL_BUFFER_PCT), 2)
        highs = sorted(
            [p.price for p in pivots if p.kind == "high" and p.price > entry]
        )
        if not highs:
            risk = entry - stop
            take_profits = [
                round(entry + risk * mult, 2) for mult in (1.5, 2.5, 3.5)
            ]
        else:
            take_profits = highs[:3]
            if len(take_profits) < 3:
                risk = entry - stop
                last = take_profits[-1] if take_profits else entry
                while len(take_profits) < 3:
                    last = round(last + risk, 2)
                    take_profits.append(last)
        return stop, take_profits

    highs = [p for p in pivots if p.kind == "high" and p.price > entry]
    if highs:
        swing = min(highs, key=lambda p: p.price)
        stop = round(swing.price * (1 + SL_BUFFER_PCT), 2)
    else:
        stop = round(entry * (1 + SL_BUFFER_PCT), 2)
    lows = sorted(
        [p.price for p in pivots if p.kind == "low" and p.price < entry],
        reverse=True,
    )
    if not lows:
        risk = stop - entry
        take_profits = [round(entry - risk * mult, 2) for mult in (1.5, 2.5, 3.5)]
    else:
        take_profits = lows[:3]
        if len(take_profits) < 3:
            risk = stop - entry
            last = take_profits[-1] if take_profits else entry
            while len(take_profits) < 3:
                last = round(last - risk, 2)
                take_profits.append(last)
    return stop, take_profits


def _ensure_min_rr(
    entry: float,
    stop: float,
    take_profits: list[float],
    direction: Direction,
) -> list[float]:
    """Extend first TP so recomputed R/R meets validate.MIN_RISK_REWARD."""
    risk = abs(entry - stop)
    if risk <= 0 or not take_profits:
        return take_profits
    min_reward = risk * validate.MIN_RISK_REWARD
    if direction == "bullish":
        min_tp = entry + min_reward
        tps = list(take_profits)
        if tps[0] < min_tp:
            tps[0] = round(min_tp, 2)
        for i in range(1, len(tps)):
            if tps[i] <= tps[i - 1]:
                tps[i] = round(tps[i - 1] + risk, 2)
        return tps
    min_tp = entry - min_reward
    tps = list(take_profits)
    if tps[0] > min_tp:
        tps[0] = round(min_tp, 2)
    for i in range(1, len(tps)):
        if tps[i] >= tps[i - 1]:
            tps[i] = round(tps[i - 1] - risk, 2)
    return tps


def _suggested_size_eth(entry: float, stop_loss: float) -> float:
    risk_usd = config.PAPER_PORTFOLIO_VALUE * validate.TARGET_RISK_PCT
    sl_pct = abs(entry - stop_loss) / entry
    if sl_pct <= 0:
        return 0.0
    return round((risk_usd / sl_pct) / entry, 4)


def _order_block_dict(ob: OrderBlock) -> dict:
    return {
        "low": ob.low,
        "high": ob.high,
        "start_ts": ob.start_ts,
        "end_ts": ob.end_ts,
    }


def _h1_ob_overlaps_h12(ob: OrderBlock, htf_zones: list[HTFZone]) -> HTFZone | None:
    for zone in htf_zones:
        if zone.mitigated or zone.zone_type != "order_block":
            continue
        if zone.direction != ob.direction:
            continue
        if zones_overlap(ob.low, ob.high, zone.low, zone.high):
            return zone
    return None


def _htf_context_lines(ctx: MarketContext, ob: OrderBlock) -> list[str]:
    lines: list[str] = []
    snap = ctx.zone_snapshot
    if snap and snap.primary_bullish:
        z = snap.primary_bullish
        lines.append(f"H12 bullish zone: {z.low:,.2f}-{z.high:,.2f}")
    if snap and snap.primary_bearish:
        z = snap.primary_bearish
        lines.append(f"H12 bearish zone: {z.low:,.2f}-{z.high:,.2f}")
    overlap = _h1_ob_overlaps_h12(ob, ctx.htf_zones)
    if overlap is not None:
        lines.append(
            f"H1 OB coincides with H12 OB {overlap.low:,.2f}-{overlap.high:,.2f}"
        )
    if ctx.range_24h:
        lines.append(
            f"24h range: {ctx.range_24h.low:,.2f}-{ctx.range_24h.high:,.2f} "
            f"(width {ctx.range_24h.width_pct:.1f}%)"
        )
    if ctx.setup_state and ctx.setup_state.phase != "idle":
        phase = ctx.setup_state.phase
        lines.append(f"Setup phase: {phase}")
    if ctx.key_levels_near:
        nearest = ", ".join(f"{lv.label} @ {lv.price:,.2f}" for lv in ctx.key_levels_near[:3])
        lines.append(f"Nearest key levels: {nearest}")
    return lines


def _build_rationale(
    trigger: WatchdogTrigger,
    ctx: MarketContext,
    ob: OrderBlock,
    entry: float,
) -> str:
    z_low, z_high = fib_zone_bounds(ob.direction, ob.low, ob.high)
    htf_lines = _htf_context_lines(ctx, ob)
    body_parts = [
        f"[Watchdog — {trigger.name}]",
        "",
        f"{trigger.reason}.",
        "",
        (
            f"Entry {entry:,.2f} inside H1 OB fib zone ({z_low:,.2f}-{z_high:,.2f}); "
            f"block bounds {ob.low:,.2f}-{ob.high:,.2f}."
        ),
    ]
    if htf_lines:
        body_parts.append("")
        body_parts.append("HTF context:")
        body_parts.extend(f"• {line}" for line in htf_lines)
    body_parts.extend(
        [
            "",
            "Programmatic intrabar scan — structure overlays on attached charts; "
            "no LLM chart review this cycle.",
        ]
    )
    body = "\n".join(body_parts)
    signals = critic.build_signals_block(ctx.alerts)
    return critic.compose_rationale(body, signals)


def _render_output_charts(
    suggestion: Suggestion,
    data: dict[str, list[dict]],
    ctx: MarketContext,
    cycle_id: str,
    daily_bars: list[dict],
) -> list[str]:
    key_levels = compute_key_levels(daily_bars)
    htf_zones = detect_htf_zones(data["H12"])
    return charts.build_output_charts(
        suggestion,
        data,
        key_levels,
        htf_zones,
        cycle_id,
        market_context=ctx,
    )


def build_suggestion(
    trigger: WatchdogTrigger,
    ctx: MarketContext,
    h12_bars: list[dict],
) -> Suggestion:
    ob = trigger.ob
    entry = round(ctx.spot, 2)
    if not price_in_ob(entry, ob):
        fib_entry = fib_level(ob.direction, ob.low, ob.high, 0.702)
        entry = fib_entry

    stop_loss, take_profits = _stop_and_targets(
        entry=entry,
        direction=trigger.direction,
        h12_bars=h12_bars,
        use_sfp_tp=trigger.use_sfp_tp,
    )
    take_profits = _ensure_min_rr(entry, stop_loss, take_profits, trigger.direction)
    action = "spot_buy" if trigger.direction == "bullish" else "spot_sell"
    size = _suggested_size_eth(entry, stop_loss)

    rationale = _build_rationale(trigger, ctx, ob, entry)

    payload = {
        "action": action,
        "size": size,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profits": take_profits,
        "rationale": rationale,
        "structure_chart": "H12",
        "entry_chart": "H1",
        "order_block": _order_block_dict(ob),
    }
    return analyze.validate_suggestion(payload, market_context=ctx)


def _is_on_cooldown(trigger_key: str) -> bool:
    state = get_state(WATCHDOG_STATE_KEY)
    if not state or state.get("trigger_key") != trigger_key:
        return False
    try:
        fired_at = datetime.fromisoformat(str(state["fired_at"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    elapsed = (datetime.now(timezone.utc) - fired_at).total_seconds()
    return elapsed < bot_config.WATCHDOG_COOLDOWN_SEC


def _record_fire(trigger_key: str, cycle_id: str) -> None:
    set_state(
        WATCHDOG_STATE_KEY,
        {
            "trigger_key": trigger_key,
            "cycle_id": cycle_id,
            "fired_at": _now_iso(),
        },
    )


def _prepare_context() -> tuple[MarketContext, dict[str, list[dict]], float, list[dict]]:
    data = research.get_all_timeframes()
    live_spot = research.get_live_spot_price()
    h1_live = research.apply_live_spot_to_h1(data["H1"], live_spot)
    daily_bars = research.get_daily_bars_for_levels()
    ctx = build_market_context(
        data["H12"],
        data["H4"],
        h1_live,
        daily_bars=daily_bars,
        spot_override=live_spot,
    )
    data["H1"] = h1_live
    return ctx, data, live_spot, daily_bars


def run_watchdog() -> Suggestion | None:
    """Run one watchdog scan. Returns a trade Suggestion when a trigger fires."""
    if not bot_config.WATCHDOG_ENABLED:
        return None

    try:
        ctx, data, live_spot, daily_bars = _prepare_context()
    except Exception:
        logger.exception("Watchdog failed to load market data")
        return None

    triggers = evaluate_triggers(ctx, data["H1"])
    if not triggers:
        logger.debug("Watchdog scan: no triggers (spot=%.2f)", live_spot)
        return None

    for trigger in triggers:
        key = _trigger_key(trigger)
        if _is_on_cooldown(key):
            logger.info("Watchdog: cooldown active for %s", key)
            continue

        try:
            suggestion = build_suggestion(trigger, ctx, data["H12"])
        except ValueError as exc:
            logger.info(
                "Watchdog trigger %s rejected: %s",
                trigger.name,
                exc,
            )
            continue

        cycle_id = _cycle_id()
        setup_tags = ",".join(ctx.setup_tags) if ctx.setup_tags else None

        output_paths: list[str] = []
        try:
            output_paths = _render_output_charts(
                suggestion, data, ctx, cycle_id, daily_bars
            )
        except Exception:
            logger.exception("Watchdog chart render failed for %s", cycle_id)

        chart_for_ledger = ",".join(output_paths) if output_paths else "watchdog"
        ledger.append(
            suggestion,
            cycle_id,
            live_spot,
            chart_path=chart_for_ledger,
            setup_tags=setup_tags,
        )
        paper.update(suggestion, live_spot, cycle_id=cycle_id)
        pnl_footer = paper.format_pnl_footer(live_spot)

        try:
            if output_paths:
                notify.broadcast(suggestion, output_paths, pnl_footer=pnl_footer)
            else:
                notify.broadcast_text(suggestion, pnl_footer=pnl_footer)
        except Exception:
            logger.exception("Watchdog broadcast failed for %s", cycle_id)

        try:
            notify.send_watchdog_monitor_alert(cycle_id, trigger.name, suggestion)
        except Exception:
            logger.exception("Watchdog monitor alert failed for %s", cycle_id)

        _record_fire(key, cycle_id)
        logger.info(
            "Watchdog trade fired: cycle=%s trigger=%s action=%s entry=%s charts=%s",
            cycle_id,
            trigger.name,
            suggestion.action,
            suggestion.entry,
            output_paths or "none",
        )
        return suggestion

    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_watchdog()
    if result:
        print(f"Fired: {result.action} @ {result.entry}")
    else:
        print("No watchdog trigger")
