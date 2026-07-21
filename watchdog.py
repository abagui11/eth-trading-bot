"""Sub-hourly programmatic entry scanner.

Runs between hourly vision cycles. When deterministic triggers fire (M5 OB fib,
bearish retest rejection, M5 SFP on close), builds and validates a trade,
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
import display_summary
import ledger
import notify
import paper
import research
import user_books
import validate
from models import Suggestion
from macro.context import active_posture
from patterns.htf_structure import HTFZone, detect_htf_zones
from patterns.key_levels import compute_key_levels
from patterns.market_context import MarketContext, build_market_context
from patterns import relative_strength
from patterns.order_block import (
    OrderBlock,
    fib_level,
    fib_zone_bounds,
    near_fib_level,
    order_block_ref,
    price_in_full_ob,
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
    deploy_pct: float | None = None
    entry_tranche: str | None = None
    stop_override: float | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cycle_id() -> str:
    return datetime.now(timezone.utc).strftime("WD%Y%m%dT%H%M%SZ")


def _trigger_key(product_id: str, trigger: WatchdogTrigger) -> str:
    sfp_ts = trigger.sfp_event.ts if trigger.sfp_event else ""
    return f"{product_id}:{trigger.name}:{trigger.ob.displacement_ts}:{sfp_ts}"


def _obs_in_fib(ctx: MarketContext, direction: Direction) -> list[OrderBlock]:
    matches = [
        ob
        for ob in ctx.order_blocks
        if ob.direction == direction and price_in_ob(ctx.spot, ob)
    ]
    return sorted(matches, key=lambda ob: ob.displacement_ts, reverse=True)


def _tranche_filled(positions: list[dict], ob_ref: str, tranche: str) -> bool:
    for pos in positions:
        if str(pos.get("order_block_ref") or "") != ob_ref:
            continue
        if tranche in (pos.get("entry_tranches") or []):
            return True
    return False


def _match_ob_by_ref(order_blocks: list[OrderBlock], ref: str) -> OrderBlock | None:
    for ob in order_blocks:
        if order_block_ref(ob) == ref:
            return ob
    return None


def _active_fib_ob_ref(positions: list[dict], direction: Direction) -> str | None:
    """If a same-side fib position is open, only that OB may add/tranche further."""
    side = "long" if direction == "bullish" else "short"
    for pos in positions:
        if str(pos.get("side") or "") != side:
            continue
        ref = str(pos.get("order_block_ref") or "")
        if not ref:
            continue
        tranches = pos.get("entry_tranches") or []
        if any(t in ("0.25", "0.50", "0.718") for t in tranches):
            return ref
        # Open same-side OB position without tranche tags still owns the slot.
        return ref
    return None


def _append_tranche_triggers(
    triggers: list[WatchdogTrigger],
    *,
    ctx: MarketContext,
    ob: OrderBlock,
    direction: Direction,
    positions: list[dict],
) -> None:
    ref = order_block_ref(ob)
    active_ref = _active_fib_ob_ref(positions, direction)
    if active_ref is not None and active_ref != ref:
        # Another M5 OB already owns this side — do not stack competing OBs.
        return
    pairs = (
        (bot_config.ENTRY_FIB_TRANCHE_1, "0.25", bot_config.ENTRY_TRANCHE_DEPLOY_PCT),
        (bot_config.ENTRY_FIB_TRANCHE_2, "0.50", bot_config.ENTRY_TRANCHE_DEPLOY_PCT),
    )
    for fib_mark, tranche, deploy_pct in pairs:
        if _tranche_filled(positions, ref, tranche):
            continue
        if tranche == "0.50" and not _tranche_filled(positions, ref, "0.25"):
            # Second half of the base position — require first tranche on this OB.
            if not any(
                str(p.get("order_block_ref") or "") == ref
                for p in positions
                if str(p.get("side")) == ("long" if direction == "bullish" else "short")
            ):
                continue
        if not near_fib_level(ctx.spot, direction, ob.low, ob.high, fib_mark):
            continue
        side = "long" if direction == "bullish" else "short"
        triggers.append(
            WatchdogTrigger(
                name=f"m5_ob_fib_{side}",
                direction=direction,
                ob=ob,
                reason=(
                    f"Price at M5 OB fib {tranche} tranche "
                    f"({fib_level(direction, ob.low, ob.high, fib_mark):,.2f})"
                ),
                priority=70,
                deploy_pct=deploy_pct,
                entry_tranche=tranche,
            )
        )
        break


def _latest_entry_bar_ts(entry_bars: list[dict]) -> str | None:
    if not entry_bars:
        return None
    return str(entry_bars[-1]["ts"])


def _sfp_on_latest_bar(event: SFPEvent, entry_bars: list[dict]) -> bool:
    latest = _latest_entry_bar_ts(entry_bars)
    if latest is None:
        return False
    return event.ts == latest


def _fresh_m5_sfp(ctx: MarketContext, m5_bars: list[dict]) -> SFPEvent | None:
    for event in reversed(ctx.m5_sfps):
        if event.outcome_a not in ("reversal", "pending"):
            continue
        if _sfp_on_latest_bar(event, m5_bars):
            return event
    return None


def evaluate_scale_in(
    ctx: MarketContext,
    positions: list[dict],
) -> WatchdogTrigger | None:
    """0.718 fib scale-in: adds another TRADE_DEPLOY_PCT to an existing OB position."""
    for pos in positions:
        ref = str(pos.get("order_block_ref") or "")
        if not ref:
            continue
        tranches = pos.get("entry_tranches") or []
        if "0.718" in tranches:
            continue
        ob = _match_ob_by_ref(ctx.order_blocks, ref)
        if ob is None:
            continue
        side = str(pos.get("side") or "")
        if (side == "long" and ob.direction != "bullish") or (
            side == "short" and ob.direction != "bearish"
        ):
            continue
        if not near_fib_level(ctx.spot, ob.direction, ob.low, ob.high, bot_config.ADD_FIB_LEVEL):
            continue
        add_level = fib_level(ob.direction, ob.low, ob.high, bot_config.ADD_FIB_LEVEL)
        return WatchdogTrigger(
            name="m5_ob_fib_add",
            direction=ob.direction,
            ob=ob,
            reason=(
                f"Scale-in at M5 OB fib 0.718 ({add_level:,.2f}) — "
                f"adds {bot_config.ADD_DEPLOY_PCT:.0%} notional to existing position"
            ),
            priority=95,
            deploy_pct=bot_config.ADD_DEPLOY_PCT,
            entry_tranche="0.718",
        )
    return None


def evaluate_triggers(
    ctx: MarketContext,
    m5_bars: list[dict],
    *,
    positions: list[dict] | None = None,
) -> list[WatchdogTrigger]:
    """Return actionable triggers sorted by priority (highest first)."""
    triggers: list[WatchdogTrigger] = []
    open_positions = positions or []

    if "short_trigger_retest" in ctx.setup_tags:
        for ob in _obs_in_fib(ctx, "bearish"):
            triggers.append(
                WatchdogTrigger(
                    name="short_trigger_retest",
                    direction="bearish",
                    ob=ob,
                    reason=(
                        "Bearish HTF retest rejection + M5 OB fib zone — "
                        "programmatic short trigger"
                    ),
                    priority=100,
                )
            )
            break

    sfp = _fresh_m5_sfp(ctx, m5_bars)
    if sfp is not None:
        direction: Direction = sfp.direction
        for ob in ctx.order_blocks:
            if ob.direction != direction:
                continue
            if direction == "bullish":
                if ctx.spot <= sfp.swept_level:
                    continue
                if not price_in_full_ob(ctx.spot, ob):
                    continue
                if price_in_ob(ctx.spot, ob):
                    continue
                stop = round(sfp.swept_level * (1 - SL_BUFFER_PCT), 2)
                triggers.append(
                    WatchdogTrigger(
                        name="m5_sfp_sweep_reversal",
                        direction="bullish",
                        ob=ob,
                        reason=(
                            f"Bullish M5 SFP sweep-reversal: reclaimed above "
                            f"{sfp.swept_level:,.2f} inside M5 OB"
                        ),
                        priority=88,
                        sfp_event=sfp,
                        stop_override=stop,
                        deploy_pct=bot_config.TRADE_DEPLOY_PCT,
                        entry_tranche="sweep",
                    )
                )
                break
            else:
                if ctx.spot >= sfp.swept_level:
                    continue
                if not price_in_full_ob(ctx.spot, ob):
                    continue
                if price_in_ob(ctx.spot, ob):
                    continue
                stop = round(sfp.swept_level * (1 + SL_BUFFER_PCT), 2)
                triggers.append(
                    WatchdogTrigger(
                        name="m5_sfp_sweep_reversal",
                        direction="bearish",
                        ob=ob,
                        reason=(
                            f"Bearish M5 SFP sweep-reversal: reclaimed below "
                            f"{sfp.swept_level:,.2f} inside M5 OB"
                        ),
                        priority=88,
                        sfp_event=sfp,
                        stop_override=stop,
                        deploy_pct=bot_config.TRADE_DEPLOY_PCT,
                        entry_tranche="sweep",
                    )
                )
                break

    if sfp is not None:
        direction = sfp.direction
        for ob in _obs_in_fib(ctx, direction):
            triggers.append(
                WatchdogTrigger(
                    name="m5_sfp_close",
                    direction=direction,
                    ob=ob,
                    reason=(
                        f"M5 {direction} SFP confirmed on latest bar close @ "
                        f"{sfp.swept_level:,.2f} with price in M5 OB entry band"
                    ),
                    priority=90,
                    use_sfp_tp=True,
                    sfp_event=sfp,
                    deploy_pct=bot_config.TRADE_DEPLOY_PCT,
                    entry_tranche="sfp",
                )
            )
            break

    if "m5_ob_bullish_in_fib" in ctx.setup_tags:
        for ob in sorted(
            [o for o in ctx.order_blocks if o.direction == "bullish"],
            key=lambda o: o.displacement_ts,
            reverse=True,
        ):
            _append_tranche_triggers(
                triggers,
                ctx=ctx,
                ob=ob,
                direction="bullish",
                positions=open_positions,
            )

    if "m5_ob_bearish_in_fib" in ctx.setup_tags:
        for ob in sorted(
            [o for o in ctx.order_blocks if o.direction == "bearish"],
            key=lambda o: o.displacement_ts,
            reverse=True,
        ):
            _append_tranche_triggers(
                triggers,
                ctx=ctx,
                ob=ob,
                direction="bearish",
                positions=open_positions,
            )

    triggers.sort(key=lambda t: t.priority, reverse=True)
    return triggers


def _swing_levels(h4_bars: list[dict]) -> list[Pivot]:
    if len(h4_bars) < 10:
        return []
    df = research.to_dataframe(h4_bars)
    return find_pivots(df)


def _stop_and_targets(
    *,
    entry: float,
    direction: Direction,
    h4_bars: list[dict],
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

    pivots = _swing_levels(h4_bars)
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


def _order_block_dict(ob: OrderBlock) -> dict:
    return {
        "low": ob.low,
        "high": ob.high,
        "start_ts": ob.start_ts,
        "end_ts": ob.end_ts,
    }


def _m5_ob_overlaps_h4(ob: OrderBlock, htf_zones: list[HTFZone]) -> HTFZone | None:
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
        lines.append(f"H4 bullish zone: {z.low:,.2f}-{z.high:,.2f}")
    if snap and snap.primary_bearish:
        z = snap.primary_bearish
        lines.append(f"H4 bearish zone: {z.low:,.2f}-{z.high:,.2f}")
    overlap = _m5_ob_overlaps_h4(ob, ctx.htf_zones)
    if overlap is not None:
        lines.append(
            f"M5 OB coincides with H4 OB {overlap.low:,.2f}-{overlap.high:,.2f}"
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
    t25 = fib_level(ob.direction, ob.low, ob.high, bot_config.ENTRY_FIB_TRANCHE_1)
    t50 = fib_level(ob.direction, ob.low, ob.high, bot_config.ENTRY_FIB_TRANCHE_2)
    t718 = fib_level(ob.direction, ob.low, ob.high, bot_config.ADD_FIB_LEVEL)
    htf_lines = _htf_context_lines(ctx, ob)
    body_parts = [
        f"[Watchdog — {trigger.name}]",
        "",
        f"{trigger.reason}.",
        "",
        (
            f"Entry {entry:,.2f}; M5 OB {ob.low:,.2f}-{ob.high:,.2f}; "
            f"entry band 0.25-0.50: {z_low:,.2f}-{z_high:,.2f}; "
            f"tranches @ {t25:,.2f}/{t50:,.2f}; add @ {t718:,.2f}."
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
    context_block = critic.build_market_context_block(ctx.alerts)
    return critic.compose_rationale(body, context_block)


def _render_output_charts(
    suggestion: Suggestion,
    data: dict[str, list[dict]],
    ctx: MarketContext,
    cycle_id: str,
    daily_bars: list[dict],
    *,
    source_ts: str | None = None,
) -> list[str]:
    key_levels = compute_key_levels(daily_bars)
    product_id = getattr(suggestion, "product_id", None) or "ETH-USD"
    htf_zones = detect_htf_zones(data["H4"], product_id=product_id)
    return charts.build_trade_broadcast_charts(
        suggestion,
        data,
        key_levels,
        htf_zones,
        cycle_id,
        market_context=ctx,
        source_ts=source_ts,
    )


def build_suggestion(
    trigger: WatchdogTrigger,
    ctx: MarketContext,
    h4_bars: list[dict],
    product_id: str = "ETH-USD",
) -> Suggestion:
    ob = trigger.ob
    if trigger.entry_tranche in ("0.25", "0.50", "0.718"):
        entry = fib_level(ob.direction, ob.low, ob.high, float(trigger.entry_tranche))
    else:
        entry = round(ctx.spot, 2)

    if trigger.stop_override is not None:
        stop_loss = trigger.stop_override
        _, take_profits = _stop_and_targets(
            entry=entry,
            direction=trigger.direction,
            h4_bars=h4_bars,
            use_sfp_tp=False,
        )
    else:
        stop_loss, take_profits = _stop_and_targets(
            entry=entry,
            direction=trigger.direction,
            h4_bars=h4_bars,
            use_sfp_tp=trigger.use_sfp_tp,
        )
    take_profits = _ensure_min_rr(entry, stop_loss, take_profits, trigger.direction)
    action = "spot_buy" if trigger.direction == "bullish" else "spot_sell"

    rationale = _build_rationale(trigger, ctx, ob, entry)

    payload = {
        "product_id": product_id,
        "action": action,
        "size": 0,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profits": take_profits,
        "rationale": rationale,
        "structure_chart": "H4",
        "entry_chart": "M5",
        "order_block": _order_block_dict(ob),
        "deploy_pct": trigger.deploy_pct,
        "entry_tranche": trigger.entry_tranche,
        "order_block_ref": order_block_ref(ob),
    }
    suggestion = analyze.validate_suggestion(payload, market_context=ctx)
    suggestion.product_id = product_id
    suggestion.deploy_pct = trigger.deploy_pct
    suggestion.entry_tranche = trigger.entry_tranche
    suggestion.order_block_ref = order_block_ref(ob)
    return suggestion


def _is_on_cooldown(trigger_key: str) -> bool:
    state = get_state(WATCHDOG_STATE_KEY)
    if not state:
        return False
    fires = state.get("fires")
    if isinstance(fires, dict):
        fired_at_raw = fires.get(trigger_key)
    elif state.get("trigger_key") == trigger_key:
        fired_at_raw = state.get("fired_at")
    else:
        return False
    if not fired_at_raw:
        return False
    try:
        fired_at = datetime.fromisoformat(str(fired_at_raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    elapsed = (datetime.now(timezone.utc) - fired_at).total_seconds()
    return elapsed < bot_config.WATCHDOG_COOLDOWN_SEC


def _record_fire(trigger_key: str, cycle_id: str) -> None:
    state = get_state(WATCHDOG_STATE_KEY) or {}
    fires = state.get("fires")
    if not isinstance(fires, dict):
        fires = {}
    now = _now_iso()
    fires[trigger_key] = now
    set_state(
        WATCHDOG_STATE_KEY,
        {
            "trigger_key": trigger_key,
            "cycle_id": cycle_id,
            "fired_at": now,
            "fires": fires,
        },
    )


def _prepare_context(
    product_id: str,
) -> tuple[MarketContext, dict[str, list[dict]], float, list[dict]]:
    data = research.get_all_timeframes(product_id=product_id)
    live_spot = research.get_live_spot_price(product_id=product_id)
    m5_live = research.apply_live_spot_to_bars(data["M5"], live_spot)
    daily_bars = research.get_daily_bars_for_levels(product_id=product_id)
    ctx = build_market_context(
        data["H4"],
        data["H1"],
        m5_live,
        daily_bars=daily_bars,
        spot_override=live_spot,
        product_id=product_id,
    )
    data["M5"] = m5_live
    return ctx, data, live_spot, daily_bars


def run_watchdog() -> list[Suggestion] | None:
    """Run one watchdog scan; fire at most once per configured product."""
    if not bot_config.WATCHDOG_ENABLED:
        return None

    spots = research.get_spot_prices()
    try:
        user_books.expire_pending_decisions()
        user_books.check_user_sl_tp(spots=spots)
        notify.process_missed_connections(spots=spots)
    except Exception:
        logger.exception("Watchdog user-book maintenance failed")

    posture = active_posture()
    rs_bias = "neutral"
    if bot_config.RELATIVE_STRENGTH_ENABLED:
        try:
            rs_bias = relative_strength.build_relative_strength_context().bias
        except Exception:
            logger.exception("Watchdog failed to build ETH/BTC relative strength")

    all_positions = paper.get_open_positions(spots=spots)
    fired: list[Suggestion] = []
    for product_id in bot_config.TRADED_PRODUCTS:
        try:
            ctx, data, live_spot, daily_bars = _prepare_context(product_id)
        except Exception:
            logger.exception("Watchdog failed to load %s market data", product_id)
            continue

        spots[product_id] = live_spot
        open_positions = [
            p
            for p in all_positions
            if p.get("product_id", "ETH-USD") == product_id
        ]
        triggers = evaluate_triggers(
            ctx, data["M5"], positions=open_positions
        )
        scale_in = evaluate_scale_in(ctx, open_positions)
        if scale_in is not None:
            triggers = [scale_in] + triggers

        for trigger in triggers:
            side = "long" if trigger.direction == "bullish" else "short"
            if (
                bot_config.RELATIVE_STRENGTH_ENABLED
                and not relative_strength.soft_gate_allows(
                    rs_bias, product_id, side
                )
            ):
                logger.info(
                    "Watchdog: ETH/BTC %s bias blocked %s %s",
                    rs_bias,
                    product_id,
                    side,
                )
                ctx.setup_tags.append("relative_strength_gate")
                continue
            if posture.get("gate_long") and trigger.direction == "bullish":
                ctx.setup_tags.append("macro_gate_long")
                continue
            if posture.get("gate_short") and trigger.direction == "bearish":
                ctx.setup_tags.append("macro_gate_short")
                continue

            key = _trigger_key(product_id, trigger)
            if _is_on_cooldown(key):
                logger.info("Watchdog: cooldown active for %s", key)
                continue

            try:
                suggestion = build_suggestion(
                    trigger, ctx, data["H4"], product_id=product_id
                )
            except ValueError as exc:
                logger.info(
                    "Watchdog trigger %s rejected for %s: %s",
                    trigger.name,
                    product_id,
                    exc,
                )
                continue

            cycle_id = (
                f"{_cycle_id()}_{bot_config.product_label(product_id).upper()}"
            )
            setup_tags = ",".join(ctx.setup_tags) if ctx.setup_tags else None
            output_paths: list[str] = []
            try:
                source_ts = None
                if trigger.sfp_event is not None:
                    source_ts = trigger.sfp_event.ts
                elif trigger.ob is not None:
                    source_ts = trigger.ob.start_ts or trigger.ob.displacement_ts
                output_paths = _render_output_charts(
                    suggestion,
                    data,
                    ctx,
                    cycle_id,
                    daily_bars,
                    source_ts=source_ts,
                )
            except Exception:
                logger.exception("Watchdog chart render failed for %s", cycle_id)

            ledger.append(
                suggestion,
                cycle_id,
                live_spot,
                chart_path=",".join(output_paths) if output_paths else "watchdog",
                setup_tags=setup_tags,
            )
            paper.update(
                suggestion,
                spots.get("ETH-USD", live_spot),
                cycle_id=cycle_id,
                spots=spots,
            )
            offer_id = None
            card_summary = None
            house_pos_id = user_books.find_house_position_id_for_cycle(cycle_id)
            try:
                card_summary = display_summary.generate_display_summary(suggestion)
            except Exception:
                logger.exception("Display summary generation failed for %s", cycle_id)
                card_summary = None
            offer = user_books.create_trade_offer(
                cycle_id=cycle_id,
                suggestion=suggestion,
                chart_paths=output_paths,
                house_position_id=house_pos_id,
                display_summary=card_summary,
            )
            if offer:
                offer_id = offer["offer_id"]
            pnl_footer = paper.format_pnl_footer(spots=spots)

            try:
                if output_paths:
                    notify.broadcast(
                        suggestion,
                        output_paths,
                        pnl_footer=pnl_footer,
                        offer_id=offer_id,
                        display_summary_text=card_summary,
                    )
                else:
                    notify.broadcast_text(
                        suggestion,
                        pnl_footer=pnl_footer,
                        offer_id=offer_id,
                        display_summary_text=card_summary,
                    )
            except Exception:
                logger.exception("Watchdog broadcast failed for %s", cycle_id)

            try:
                notify.send_watchdog_monitor_alert(
                    cycle_id, trigger.name, suggestion
                )
            except Exception:
                logger.exception("Watchdog monitor alert failed for %s", cycle_id)

            _record_fire(key, cycle_id)
            fired.append(suggestion)
            logger.info(
                "Watchdog trade fired: cycle=%s product=%s trigger=%s "
                "action=%s entry=%s charts=%s",
                cycle_id,
                product_id,
                trigger.name,
                suggestion.action,
                suggestion.entry,
                output_paths or "none",
            )
            break

    return fired or None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_watchdog()
    if result:
        for suggestion in result:
            print(
                f"Fired: {suggestion.product_id} "
                f"{suggestion.action} @ {suggestion.entry}"
            )
    else:
        print("No watchdog trigger")
