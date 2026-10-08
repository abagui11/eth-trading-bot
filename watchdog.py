"""Sub-hourly programmatic entry scanner.

Runs between hourly vision cycles. When deterministic triggers fire (M5 OB fib,
bearish retest rejection, M5 SFP on close), builds and validates a trade,
renders structure/entry charts, then records and broadcasts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

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


def _position_unrealized_r(pos: dict, spot: float) -> float | None:
    """Signed R multiples vs entry→stop distance (positive = in profit)."""
    try:
        entry = float(pos["avg_entry"])
        stop = float(pos["stop_loss"])
    except (KeyError, TypeError, ValueError):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    side = str(pos.get("side") or "")
    if side == "long":
        return (float(spot) - entry) / risk
    if side == "short":
        return (entry - float(spot)) / risk
    return None


def evaluate_scale_in(
    ctx: MarketContext,
    positions: list[dict],
) -> WatchdogTrigger | None:
    """0.718 fib scale-in when an existing OB position is at least SCALE_IN_MIN_R in profit."""
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
        unrealized_r = _position_unrealized_r(pos, ctx.spot)
        if unrealized_r is None or unrealized_r < bot_config.SCALE_IN_MIN_R:
            logger.info(
                "Watchdog: scale-in blocked underwater (R=%.2f, need >= %.2f) ref=%s",
                unrealized_r if unrealized_r is not None else float("nan"),
                bot_config.SCALE_IN_MIN_R,
                ref,
            )
            if "scale_in_blocked_underwater" not in ctx.setup_tags:
                ctx.setup_tags.append("scale_in_blocked_underwater")
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


def _ensure_min_stop_distance(
    entry: float,
    stop: float,
    direction: Direction,
) -> float:
    """Widen stop to validate.MIN_STOP_DISTANCE_PCT when structural stop is too tight."""
    floor = entry * validate.MIN_STOP_DISTANCE_PCT
    if direction == "bullish":
        max_stop = entry - floor
        if stop > max_stop:
            return round(max_stop, 2)
        return stop
    min_stop = entry + floor
    if stop < min_stop:
        return round(min_stop, 2)
    return stop


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
        stop = _ensure_min_stop_distance(entry, stop, direction)
        return stop, [tp]

    pivots = _swing_levels(h4_bars)
    if direction == "bullish":
        lows = [p for p in pivots if p.kind == "low" and p.price < entry]
        if lows:
            swing = max(lows, key=lambda p: p.price)
            stop = round(swing.price * (1 - SL_BUFFER_PCT), 2)
        else:
            stop = round(entry * (1 - SL_BUFFER_PCT), 2)
        stop = _ensure_min_stop_distance(entry, stop, direction)
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
    stop = _ensure_min_stop_distance(entry, stop, direction)
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
        stop_loss = _ensure_min_stop_distance(
            entry, trigger.stop_override, trigger.direction
        )
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
        "trigger_name": trigger.name,
    }
    suggestion = analyze.validate_suggestion(payload, market_context=ctx)
    suggestion.product_id = product_id
    suggestion.deploy_pct = trigger.deploy_pct
    suggestion.entry_tranche = trigger.entry_tranche
    suggestion.order_block_ref = order_block_ref(ob)
    suggestion.trigger_name = trigger.name
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


_POOL_RECON_INTERVAL_SEC = 600
_pool_last_recon = 0.0


def _payout_sweep() -> None:
    """Send one approved payout per pass. One, deliberately.

    Coinbase rejects `idem` on sends, so there is no venue-side idempotency:
    a resend is a second real payment and nothing at the far end collapses the
    two. That removes the usual safety net, so this is built to fail closed —
    one payout in flight at a time, intent recorded before the call, and an
    ambiguous outcome halts the queue for a human rather than guessing.
    """
    import notify
    import pool

    if not bot_config.POOL_ENABLED or not bot_config.POOL_PAYOUTS_ENABLED:
        return
    if pool.payouts_halted():
        return

    queued = pool.pending_withdrawals("approved")
    if not queued:
        return
    row = queued[0]
    wid = int(row["id"])
    uid = int(row["telegram_id"])
    amount = float(row["amount_usd"])
    address = str(row["to_address"])

    try:
        import payouts
    except Exception:
        logger.exception("payout sweep: client unavailable")
        return

    # Claim the row before touching the network. If the process dies after
    # this, 'submitting' is the only sign a payment might exist.
    if not pool.mark_withdrawal_submitting(wid):
        return

    # Rail 1: the test wallet, when it holds the money on a chain this user
    # has proven their address on. Undeployed capital lives there under the
    # deploy-routing rule, so without this rail a tester who never deployed
    # could only be paid from house float at Coinbase.
    if _payout_from_test_wallet(wid, uid, amount, address):
        return

    # Rail 2: Coinbase (deployed-to-Coinbase capital, and the fallback).
    try:
        account = payouts.usdc_account()
    except Exception as exc:
        # Nothing was sent, so this is safely reversible.
        pool.mark_withdrawal_failed(wid, reason=f"could not read account: {exc}")
        _notify_payout_failed(uid, wid, amount, str(exc))
        return

    if account["balance"] < amount:
        pool.mark_withdrawal_failed(
            wid, reason=f"venue balance ${account['balance']:.2f} < ${amount:.2f}"
        )
        _notify_payout_failed(uid, wid, amount, "insufficient venue balance")
        return

    try:
        sent = payouts.send(
            account_id=account["id"], to_address=address,
            amount_usd=amount, ref=f"withdrawal:{wid}",
        )
    except payouts.PayoutError as exc:
        if exc.submitted:
            # The request reached Coinbase and we do not know the outcome.
            # Refunding might hand back money that left; retrying might send
            # it twice. Neither is acceptable, so a human reconciles.
            pool.mark_withdrawal_unknown(wid, reason=str(exc))
            try:
                notify.send_pool_admin_alert(
                    f"PAYOUTS HALTED — withdrawal #{wid} outcome UNKNOWN.\n"
                    f"${amount:,.2f} to {address}\n{str(exc)[:200]}\n\n"
                    "The send may or may not have happened, and Coinbase "
                    "offers no way to ask. Check the USDC balance and the "
                    "destination on-chain before doing anything.\n"
                    "Do NOT resend. Clear with /payouts resume once settled."
                )
            except Exception:
                logger.exception("unknown-payout alert failed")
            return
        pool.mark_withdrawal_failed(wid, reason=str(exc))
        _notify_payout_failed(uid, wid, amount, str(exc))
        return
    except Exception as exc:
        pool.mark_withdrawal_unknown(wid, reason=f"unexpected: {exc}")
        return

    result = pool.mark_withdrawal_submitted(
        wid, cb_tx_id=sent["id"], fee_usd=sent["fee_usd"], txid=sent.get("txid"),
    )
    fee = float(result.get("fee_usd") or 0)
    refund = float(result.get("refunded_usd") or 0)

    lines = [
        f"Withdrawal sent: ${amount:,.2f} USDC.",
        f"To: {address}",
        f"Network fee: ${fee:,.2f}.",
    ]
    if refund >= 0.01:
        lines.append(f"Fee reserve returned: ${refund:,.2f}.")
    # Measured, not estimated: the one real payout was in the destination
    # wallet 58s after the send returned. Quoted loosely anyway — a tester who
    # is told "a few minutes" and waits ten is fine, one told "one minute" and
    # kept waiting three starts wondering where their money went.
    lines.append(
        "It is on its way — usually a few minutes. We will message you again "
        "with the transaction once it lands in your wallet."
    )
    try:
        notify.send_pool_dm(uid, "\n".join(lines))
    except Exception:
        logger.exception("payout DM failed for %s", uid)

    try:
        notify.send_pool_admin_alert(
            f"Paid out ${amount:,.2f} to {uid} (#{wid}), fee ${fee:,.2f}.\n"
            f"coinbase tx {sent['id']}"
        )
    except Exception:
        logger.exception("payout admin FYI failed")


def _payout_from_test_wallet(wid: int, uid: int, amount: float, address: str) -> bool:
    """Try to pay withdrawal `wid` from the test wallet via the signer.

    Returns True when the withdrawal is now in a terminal-for-this-pass state
    (submitted, or unknown + halted) and the Coinbase rail must NOT run.
    Returns False when nothing was sent and Coinbase should take it — the
    signer is off, the wallet has no balance on a provable chain, or a
    pre-broadcast check refused. The row stays 'submitting' across the hand-off
    so the claim made above still holds.
    """
    import notify
    import pool

    try:
        import chain
        import signer

        if not signer.enabled():
            return False
        wallet = config.TEST_WALLET_ADDRESS
        if not wallet:
            return False
        chosen: int | None = None
        for cid in pool.payout_chains_for(uid):
            if not chain.rpc_url(cid):
                continue
            try:
                held = chain._usdc_balance_rpc(wallet, chain_id=cid)
            except chain.ChainError:
                continue
            if held + 1e-9 >= amount:
                chosen = cid
                break
        if chosen is None:
            return False
    except Exception:
        logger.exception("payout #%s: test-wallet rail check failed", wid)
        return False

    try:
        sent = signer.send_usdc_payout(uid, amount, chain_id=chosen)
    except signer.SignerError as exc:
        if exc.submitted:
            # Same rule as Coinbase: a send that may have happened is not
            # refunded and not retried. Halt and let a human look.
            pool.mark_withdrawal_unknown(wid, reason=f"signer: {exc}")
            try:
                notify.send_pool_admin_alert(
                    f"PAYOUTS HALTED — withdrawal #{wid} outcome UNKNOWN "
                    f"(test-wallet rail).\n${amount:,.2f} to {address} on "
                    f"{chain.chain_name(chosen)}\n{str(exc)[:200]}\n\n"
                    f"Check the test wallet's USDC on {chain.chain_name(chosen)} "
                    "and the destination before doing anything. Do NOT resend. "
                    "Clear with /payouts resume once settled."
                )
            except Exception:
                logger.exception("unknown-payout alert failed")
            return True
        # Refused before anything left: gas, balance race, estimate. Let
        # Coinbase take it this pass rather than failing the tester.
        logger.warning("payout #%s: signer refused (%s) — falling back to coinbase", wid, exc)
        return False
    except Exception as exc:
        pool.mark_withdrawal_unknown(wid, reason=f"signer unexpected: {exc}")
        logger.exception("payout #%s: signer crashed", wid)
        return True

    # Gas is paid by the house in ETH; the tester is not charged a fee on
    # this rail, so the whole reserve goes back.
    result = pool.mark_withdrawal_submitted(
        wid, cb_tx_id=f"signer:{sent['txid']}", fee_usd=0.0, txid=sent["txid"],
        source="test_wallet", chain_id=chosen,
    )
    refund = float(result.get("refunded_usd") or 0)
    lines = [
        f"Withdrawal sent: ${amount:,.2f} USDC on {chain.chain_name(chosen)}.",
        f"To: {address}",
        f"Transaction: {sent['explorer']}",
    ]
    if refund >= 0.01:
        lines.append(f"Fee reserve returned: ${refund:,.2f} (no fee on this send).")
    lines.append(
        "It is on its way — usually a few minutes. We will message you again "
        "once it is confirmed in your wallet."
    )
    try:
        notify.send_pool_dm(uid, "\n".join(lines))
    except Exception:
        logger.exception("payout DM failed for %s", uid)
    try:
        notify.send_pool_admin_alert(
            f"Paid out ${amount:,.2f} to {uid} (#{wid}) from the test wallet on "
            f"{chain.chain_name(chosen)}.\n{sent['explorer']}"
        )
    except Exception:
        logger.exception("payout admin FYI failed")
    return True


def _notify_payout_failed(uid: int, wid: int, amount: float, why: str) -> None:
    import notify

    try:
        notify.send_pool_dm(
            uid,
            f"Withdrawal #{wid} could not be sent, so nothing left your "
            f"balance — ${amount:,.2f} has been returned in full.\n\n"
            "Nothing is lost; try again or ask an admin.",
        )
    except Exception:
        logger.exception("payout failure DM failed for %s", uid)
    try:
        notify.send_pool_admin_alert(
            f"Withdrawal #{wid} FAILED and was refunded: {why[:200]}"
        )
    except Exception:
        logger.exception("payout failure alert failed")


def _wallet_verify_sweep() -> None:
    """Prove registered wallets against the chain, and say so.

    This is what makes return-to-source a guarantee rather than a promise.
    Coinbase reports that money arrived but never who sent it, so the only
    evidence a tester controls the address they gave us is the sender on the
    transfer itself — and until that is checked, `payout_target` refuses every
    withdrawal as `unverified`.

    Never raises: this runs on the same 60s pass as stop-loss monitoring, and
    a missed verification is a delayed withdrawal, while an exception here
    would be an unwatched position.
    """
    import notify
    import pool

    if not bot_config.POOL_ENABLED or not config.POOL_DEPOSIT_ADDRESS:
        return

    try:
        import chain

        if not chain.configured():
            return
        events = pool.verify_wallets_onchain(
            chain.verify_deposit, deposit_address=config.POOL_DEPOSIT_ADDRESS
        )
    except Exception:
        logger.exception("wallet verify sweep failed — skipped")
        return

    for event in events:
        uid = int(event["telegram_id"])
        if event["kind"] == "verified":
            try:
                notify.send_pool_dm(
                    uid,
                    "Your wallet is verified.\n\n"
                    f"{event['address']}\n\n"
                    "We confirmed on-chain that your deposit came from this "
                    "address, so it is the only place withdrawals can go. "
                    "Use /withdraw whenever you like.",
                )
            except Exception:
                logger.exception("wallet verified DM failed for %s", uid)
            continue

        # Not proven. Overwhelmingly this is an honest tester who funded from
        # an exchange, so the tester is told what to do about it rather than
        # just refused, and the admin can vouch for them if the money is
        # genuinely theirs.
        try:
            notify.send_pool_dm(
                uid,
                "We could not verify your wallet yet.\n\n"
                "Your deposit is credited and safe — this only affects "
                "withdrawals. The funds did not arrive from the address you "
                f"registered ({event['address']}), which usually means they "
                "were sent from an exchange account rather than your own "
                "wallet.\n\n"
                "We only pay out to an address you have proven you control, "
                "so an admin will be in touch to sort this out.",
            )
        except Exception:
            logger.exception("wallet mismatch DM failed for %s", uid)
        try:
            notify.send_pool_admin_alert(
                f"WALLET UNPROVEN for {uid} — withdrawals blocked.\n"
                f"Registered: {event['address']}\n"
                f"Actual sender: {event.get('sender') or 'unknown'}\n"
                f"tx {event['txid']}\n\n"
                "Likely an exchange withdrawal. They cannot withdraw until "
                "this resolves — have them deposit from their own wallet, or "
                "re-register the address the funds actually came from."
            )
            pool.mark_wallet_check_alerted(str(event["txid"]),
                                           str(event["address"]))
        except Exception:
            logger.exception("wallet mismatch alert failed")


def _settle_sweep() -> None:
    """Confirm submitted payouts actually landed, by looking at the chain.

    Coinbase will not return a payout's status — `get_transaction` 404s even
    with transfer scope — so the destination address is the only independent
    evidence the money arrived. Without this a payout sits in `submitted`
    forever and "did she get it?" is answered by watching our own balance
    drop, which proves the money left but not where it went.
    """
    import notify
    import pool

    if not bot_config.POOL_ENABLED:
        return

    try:
        import chain
    except Exception:
        logger.exception("settle sweep: chain unavailable")
        return

    for row in pool.pending_withdrawals("submitted"):
        wid = int(row["id"])
        amount = float(row["amount_usd"])
        try:
            if str(row.get("source") or "coinbase") == "test_wallet" and row.get("txid"):
                # Signer payouts carry their own hash and chain: confirm the
                # receipt itself, which works over RPC where Etherscan does not.
                cid = int(row.get("chain_id") or chain.CHAIN_ID)
                if not chain.readable(cid):
                    continue
                hit = chain.find_transfer(
                    str(row["txid"]), to_address=str(row["to_address"]), chain_id=cid
                )
                found = (
                    {"ok": True, "txid": hit["txid"]}
                    if hit and hit.get("confirmations", 0) >= chain.MIN_CONFIRMATIONS
                    else {"ok": False, "reason": "not_seen_yet"}
                )
            else:
                if not chain.configured():
                    continue
                since = _epoch(row["submitted_at"])
                found = chain.confirm_payout(
                    str(row["to_address"]), amount, after_timestamp=since
                )
        except Exception:
            logger.exception("settle sweep: lookup failed for #%s", wid)
            continue

        if not found.get("ok"):
            continue

        pool.mark_withdrawal_settled(wid, txid=found.get("txid"))
        try:
            notify.send_pool_dm(
                int(row["telegram_id"]),
                f"Withdrawal confirmed: ${amount:,.2f} USDC has landed in "
                f"{row['to_address']}.\n\n"
                f"Transaction: {found.get('txid')}",
            )
        except Exception:
            logger.exception("settle DM failed for #%s", wid)
        logger.info("pool: withdrawal #%s confirmed on-chain", wid)


def _epoch(stamp: Any) -> int:
    """ISO stamp to unix seconds; 0 if unreadable.

    0 widens the match window rather than narrowing it, which risks matching
    an older transfer of the same size instead of missing a real settlement —
    the failure that leaves a tester wondering where their money went.
    """
    try:
        return int(
            datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc).timestamp()
        )
    except (TypeError, ValueError):
        return 0


def _moonpay_deposit_poll() -> None:
    """Backup crediting path when a MoonPay webhook is missed."""
    import moonpay
    import notify
    import pool

    if not bot_config.POOL_ENABLED or not moonpay.configured():
        return
    for tx in moonpay.list_recent_deposit_txs(limit=40):
        status = str(
            ((tx.get("meta") or {}).get("transactionStatus"))
            or tx.get("status")
            or ""
        ).upper()
        if status and status not in ("SUCCESS", "CONFIRMED", "COMPLETE", ""):
            continue
        customer_id = str(
            tx.get("customerId")
            or (tx.get("meta") or {}).get("customerId")
            or ""
        )
        telegram_id = pool.find_telegram_id_by_moonpay_customer(customer_id)
        if telegram_id is None and customer_id.startswith("tg_"):
            try:
                telegram_id = int(customer_id[3:])
            except ValueError:
                continue
        if telegram_id is None:
            continue
        amount = moonpay.parse_deposit_amount_usd(tx)
        if amount <= 0:
            # Treat top-level amount with 6 dec USDC assumption
            try:
                amount = round(float(tx.get("amount") or 0) / 1_000_000.0, 2)
            except (TypeError, ValueError):
                continue
        tx_key = str(
            tx.get("txIdempotencyKey")
            or (tx.get("meta") or {}).get("transactionSignature")
            or tx.get("id")
            or ""
        )
        if not tx_key:
            continue
        result = pool.credit_moonpay_deposit(
            telegram_id=telegram_id,
            amount_usd=amount,
            tx_key=tx_key,
            note="MoonPay poll",
        )
        if result.get("ok"):
            notify.send_pool_dm(
                telegram_id,
                f"Deposit received: ${amount:,.2f} USDC.\n"
                f"Wallet balance: ${float(result.get('cash_usd') or 0):,.2f}.",
            )


def _deposit_sweep() -> None:
    """Credit arrived deposits and tell the tester, without waiting on anyone.

    Runs on the 60s scan, so a tester hears back within a minute of Coinbase
    settling their transfer rather than whenever an admin next looks at their
    phone. The admin is still told — they are just no longer in the way.

    Never raises: a deposit watcher that can take down the watchdog would stop
    stop-loss monitoring, which is a far worse failure than a late credit.
    """
    import notify
    import pool

    if not bot_config.POOL_ENABLED or not config.POOL_DEPOSIT_ADDRESS:
        return

    try:
        from coinbase_deriv import get_gateway

        transfers = get_gateway().get_inbound_transfers(config.POOL_DEPOSIT_ADDRESS)
    except Exception:
        logger.exception("deposit sweep: could not read transfers — skipped")
        return

    try:
        events = pool.observe_chain_deposits(transfers)
    except Exception:
        logger.exception("deposit sweep: crediting failed")
        return

    for event in events:
        if event["kind"] == "credited":
            amount = float(event["amount_usd"])
            # The tester first, before the admin FYI: the whole point of this
            # path is that their money is confirmed the moment it is real.
            lines = [
                f"Deposit received: ${amount:,.2f} USDC.",
                f"Cash balance: ${float(event['cash_usd']):,.2f}.",
            ]
            if event.get("mismatch"):
                lines.append(
                    f"(You said ${float(event['claimed_usd']):,.2f} — we credited "
                    "what actually arrived.)"
                )
            lines.append(
                "This was confirmed automatically against the exchange, not by "
                "hand. You can Accept trade cards now — /portfolio any time."
            )
            try:
                notify.send_pool_dm(int(event["telegram_id"]), "\n".join(lines))
            except Exception:
                logger.exception("deposit DM failed for %s", event["telegram_id"])

            mismatch = (
                f"\nClaimed ${float(event['claimed_usd']):,.2f}, arrived "
                f"${amount:,.2f} — CHECK THIS."
                if event.get("mismatch") else ""
            )
            try:
                notify.send_pool_admin_alert(
                    f"Auto-credited ${amount:,.2f} to {event['telegram_id']} "
                    f"(request #{event['request_id']}).{mismatch}\n"
                    f"coinbase tx {event['cb_tx_id']}"
                )
            except Exception:
                logger.exception("deposit admin FYI failed")

        elif event["kind"] == "unmatched" and not event.get("alerted"):
            # Somebody's money is on the venue and nothing says whose. It is
            # deliberately not apportioned — a guess here credits one tester
            # with another's funds.
            try:
                notify.send_pool_admin_alert(
                    f"UNCLAIMED deposit: ${float(event['amount_usd']):,.2f} USDC "
                    "arrived with no matching /deposit claim.\n"
                    f"coinbase tx {event['cb_tx_id']}\n"
                    f"tx hash {event.get('txid')}\n\n"
                    "Nobody has been credited. If you know whose it is, assign "
                    "it with:\n"
                    f"/assign {event['cb_tx_id']} <telegram_id>"
                )
                pool.mark_chain_deposit_alerted(str(event["cb_tx_id"]))
            except Exception:
                logger.exception("unmatched deposit alert failed")


def _testwallet_deposit_sweep() -> None:
    """Credit USDC arriving at the shared Phase 1 test wallet, by sender.

    Same contract as `_deposit_sweep`: the user hears the moment the money is
    real, admins are FYI, and an arrival nobody can be matched to is flagged
    once and never apportioned. Never raises — a deposit watcher must not be
    able to take down stop-loss monitoring.
    """
    import chain
    import notify
    import pool

    if not bot_config.POOL_ENABLED or not config.TEST_WALLET_ADDRESS:
        return

    address = str(config.TEST_WALLET_ADDRESS)
    # One EOA, every listed chain: a tester may send on Base or Ethereum and
    # both land in the same wallet. Each chain is read and credited on its
    # own so one source being down never hides deposits on the other.
    for chain_id in config.TEST_WALLET_CHAIN_IDS:
        chain_id = int(chain_id)
        if not chain.readable(chain_id):
            continue
        cursor_key = f"testwallet_scan_block:{chain_id}"
        cursor_raw = pool.get_meta(cursor_key)
        cursor = int(cursor_raw) if cursor_raw and cursor_raw.isdigit() else None
        try:
            transfers, next_cursor = chain.recent_inbound_usdc(
                address, chain_id=chain_id, cursor_block=cursor, limit=50,
            )
        except Exception:
            logger.exception(
                "test-wallet sweep: %s read failed — skipped this pass",
                chain.chain_name(chain_id),
            )
            continue
        settled = [
            t for t in transfers if t["confirmations"] >= chain.MIN_CONFIRMATIONS
        ]

        try:
            events = pool.observe_testwallet_deposits(settled, chain_id=chain_id)
        except Exception:
            logger.exception("test-wallet sweep: crediting failed (%s)",
                             chain.chain_name(chain_id))
            continue
        if next_cursor is not None:
            try:
                pool.set_meta(cursor_key, str(int(next_cursor)))
            except Exception:
                logger.exception("test-wallet sweep: cursor write failed")

        _announce_testwallet_events(events, chain_id)


def _announce_testwallet_events(events: list[dict], chain_id: int) -> None:
    import chain
    import notify
    import pool

    network = chain.chain_name(chain_id)
    for event in events:
        if event["kind"] == "credited":
            amount = float(event["amount_usd"])
            try:
                notify.send_pool_dm(
                    int(event["telegram_id"]),
                    f"Deposit received: ${amount:,.2f} USDC on {network}.\n"
                    f"Cash balance: ${float(event['cash_usd']):,.2f}.\n\n"
                    "Matched to you by the wallet it came from and confirmed "
                    "on-chain — that wallet is now verified for withdrawals. "
                    "You can Accept trade cards now — /portfolio any time.",
                )
            except Exception:
                logger.exception(
                    "test-wallet deposit DM failed for %s", event["telegram_id"]
                )
            try:
                notify.send_pool_admin_alert(
                    f"Test wallet ({network}): auto-credited ${amount:,.2f} to "
                    f"{event['telegram_id']} (tx {event['txid']})."
                )
            except Exception:
                logger.exception("test-wallet admin FYI failed")
        elif event["kind"] == "unmatched" and not event.get("alerted"):
            try:
                notify.send_pool_admin_alert(
                    f"UNCLAIMED test-wallet deposit on {network}: "
                    f"${float(event['amount_usd']):,.2f} USDC "
                    f"({event.get('reason')}).\n"
                    f"tx {event['txid']}\nfrom {event.get('sender')}\n\n"
                    "Nobody has been credited. An unregistered sender is "
                    "usually an exchange withdrawal (Coinbase.com, Binance, "
                    "Kraken…): the money can be assigned, but the user cannot "
                    "withdraw until a deposit arrives from their own wallet.\n"
                    f"/assign {event['txid']} <telegram_id>"
                )
                pool.mark_testwallet_deposit_alerted(str(event["txid"]))
            except Exception:
                logger.exception("unmatched test-wallet alert failed")


# Kalshi 'placing' rows already alerted this process lifetime — the row has
# no alerted column because the state should be rare and short-lived; a
# restart re-alerting is the right failure direction.
_kalshi_placing_alerted: set[int] = set()

# (telegram_id, strategy, reason) money-blockers already nudged this process
# lifetime — autopilot hits every 15-minute window, so a user with nothing
# allocated hears about it once, not fifty times a day.
_autopilot_nudged: set[tuple[int, str, str]] = set()


def run_kalshi_autopilot() -> None:
    """Enter autopilot users into fresh Kalshi entries, and tell them.

    Runs on its own fast job (`KALSHI_AUTOPILOT_POLL_SEC`, seconds), not the
    60s watchdog scan — the house bot fires on the quarter-hour and every
    second of detection lag is slip against its entry, so waiting for the
    scan's turn cost real windows (38s late on the 2026-10-06 18:05 entry).

    Fills get a DM each time (their money moved); the money-blockers —
    nothing allocated, budget too small — get one nudge per process lifetime;
    market-shaped refusals (slipped, stale, unfilled) stay silent because the
    next window is minutes away and the user can do nothing about them.
    Never raises — it shares an executor with everything else.
    """
    import kalshi_execute
    import notify
    import strategy_catalog

    if not bot_config.POOL_ENABLED:
        return
    try:
        results = kalshi_execute.autopilot_sweep()
    except Exception:
        logger.exception("kalshi autopilot sweep failed")
        return
    for r in results:
        uid = int(r["telegram_id"])
        strategy = str(r["strategy"])
        strat = strategy_catalog.get(strategy)
        label = strat.label if strat else strategy
        if r.get("ok"):
            try:
                notify.send_pool_dm(
                    uid,
                    f"Autopilot entered you — {int(r['contracts'])} × "
                    f"{str(r['side']).upper()} on {r['market_ticker']} at an "
                    f"average {float(r['avg_cents']):.0f}¢ "
                    f"(${float(r['cost_usd']):,.2f} all-in, fee included).\n\n"
                    "The window settles on the quarter hour — you'll get the "
                    "result here either way. /portfolio any time.",
                )
            except Exception:
                logger.exception("autopilot fill DM failed for %s", uid)
            continue
        reason = str(r.get("reason"))
        logger.info(
            "autopilot: %s/%s on #%s refused (%s)",
            uid, strategy, r.get("position_id"), reason,
        )
        if reason not in (
            "no_allocation", "budget_too_small", "not_funded",
            "below_min_equity",
        ):
            continue
        nudge_key = (uid, strategy, reason)
        if nudge_key in _autopilot_nudged:
            continue
        _autopilot_nudged.add(nudge_key)
        detail = (
            f"nothing is deployed to this lane — /allocate {strategy} <amount>"
            if reason == "no_allocation" else
            "your deployment can't cover one contract at current prices — "
            f"top it up with /allocate {strategy} <amount>"
            if reason == "budget_too_small" else
            "your account isn't funded — tap Fund to top up"
            if reason == "not_funded" else
            "your balance is under the account minimum"
        )
        try:
            notify.send_pool_dm(
                uid,
                f"Autopilot is armed for {label}, but it couldn't size an "
                f"entry: {detail}. It stays on and takes the next window "
                "once that's fixed.",
            )
        except Exception:
            logger.exception("autopilot nudge DM failed for %s", uid)


def _kalshi_settle_sweep() -> None:
    """Book settled Kalshi windows to their owners, and page on stuck rows."""
    import kalshi_execute
    import notify
    import pool

    if not bot_config.POOL_ENABLED:
        return

    try:
        settled = kalshi_execute.settle_sweep()
    except Exception:
        logger.exception("kalshi settle sweep failed")
        settled = []
    for result in settled:
        pnl = float(result["pnl_usd"])
        won = pnl > 0
        try:
            notify.send_pool_dm(
                int(result["telegram_id"]),
                (
                    f"Kalshi window settled {str(result.get('side', '')).upper()}"
                    f" — {result['market_ticker']}.\n"
                    + (
                        f"You won ${pnl:,.2f} on {result['contracts']} contracts."
                        if won else
                        f"Contracts settled worthless: −${abs(pnl):,.2f}."
                        if pnl < 0 else
                        "Voided — refunded in full, exactly flat."
                    )
                    + "\n/portfolio any time."
                ),
            )
        except Exception:
            logger.exception(
                "kalshi settle DM failed for %s", result["telegram_id"]
            )

    try:
        recovered = kalshi_execute.recover_placing_sweep()
    except Exception:
        logger.exception("kalshi placing recover failed")
        recovered = []
    for result in recovered:
        if int(result.get("contracts") or 0) <= 0:
            continue
        try:
            notify.send_pool_dm(
                int(result["telegram_id"]),
                f"Autopilot entered you — {int(result['contracts'])} × "
                f"{str(result['side']).upper()} on {result['market_ticker']} at an "
                f"average {float(result['avg_cents']):.0f}¢ "
                f"(${float(result['cost_usd']):,.2f} all-in, fee included).\n\n"
                "The window settles on the quarter hour — you'll get the "
                "result here either way. /portfolio any time.",
            )
        except Exception:
            logger.exception(
                "kalshi recover fill DM failed for %s", result.get("telegram_id")
            )

    try:
        stuck = pool.stale_kalshi_placing(
            minutes=float(bot_config.KALSHI_ACCEPT_MAX_AGE_MIN)
        )
    except Exception:
        logger.exception("kalshi stale-placing check failed")
        return
    for row in stuck:
        row_id = int(row["id"])
        if row_id in _kalshi_placing_alerted:
            continue
        # Still placing after recover — now page a human.
        _kalshi_placing_alerted.add(row_id)
        try:
            notify.send_pool_admin_alert(
                f"KALSHI POSITION STUCK IN 'placing': #{row_id} user "
                f"{row['telegram_id']} {row['market_ticker']} "
                f"{row['side']} x{row['contracts']} "
                f"(${float(row['cost_usd']):,.2f} reserved, order "
                f"{row.get('order_id') or 'unknown'}).\n"
                "An order may exist at the venue unbooked. Check Kalshi, then "
                "book it via pool.finish_kalshi_open(...) or release with "
                "filled_contracts=0. The reserve is deliberately NOT "
                "auto-released."
            )
        except Exception:
            logger.exception("kalshi stuck-placing alert failed")


def _treasury_confirm_sweep() -> None:
    """Close out 'sent' treasury transfers the chain can prove arrived."""
    import notify
    import treasury

    try:
        confirmed = treasury.confirm_sweep()
    except Exception:
        logger.exception("treasury confirm sweep failed")
        return
    for transfer in confirmed:
        try:
            notify.send_pool_admin_alert(
                f"Treasury transfer #{transfer['id']} confirmed on-chain: "
                f"{transfer['from_loc']} → {transfer['to_loc']} "
                f"${float(transfer['amount_usd']):,.2f}."
            )
        except Exception:
            logger.exception("treasury confirm FYI failed")


def _treasury_deploy_retry_sweep() -> None:
    """Auto-retry deploy legs stuck in pending_send (usually after a gas fix)."""
    import notify
    import treasury

    try:
        results = treasury.retry_pending_deploy_sends(admin_id=0)
    except Exception:
        logger.exception("treasury deploy-retry sweep failed")
        return
    for row in results:
        tid = row.get("transfer_id")
        if row.get("ok"):
            gas = row.get("gas_topup") or {}
            gas_bit = (
                f" (gas top-up ${float(gas.get('usdc_spent') or 0):,.2f} USDC→ETH)"
                if gas.get("topped_up") else ""
            )
            try:
                notify.send_pool_admin_alert(
                    f"Auto-retried deploy transfer #{tid}: "
                    f"${float(row.get('amount_usd') or 0):,.2f} → "
                    f"{row.get('to_loc')}{gas_bit}.\n"
                    f"tx {row.get('txid')}"
                )
            except Exception:
                logger.exception("deploy-retry FYI failed for #%s", tid)
        else:
            # Stay quiet on still-blocked retries — the original Send card
            # already explained why. Log only.
            logger.info(
                "deploy-retry #%s still blocked: %s",
                tid, row.get("detail") or row.get("reason"),
            )


def _pool_sweep(spots: dict[str, float] | None = None) -> None:
    """Tester-pool upkeep on the scan cadence.

    1. Intents whose ref can no longer fire (pending plan replaced/expired,
       mill idea filled without them, order refused) get their reserve back
       and the tester a DM — an Accept must never just go quiet.
    2. Every ~10 minutes, check the venue's whole-account cash covers the sum
       of tester claims. A shortfall freezes NEW intents and alerts ops;
       balances are never touched, and an unreadable balance skips the check
       rather than failing it.
    3. MoonPay deposit poll (backup if webhooks miss).
    """
    import time as _time

    import live_pending
    import notify
    import pool
    import trade_ideas_bridge

    try:
        _moonpay_deposit_poll()
    except Exception:
        logger.exception("MoonPay deposit poll failed")

    active: set[str] = {
        str(r.get("cycle_id") or "") for r in live_pending.get_pending()
    }
    active |= trade_ideas_bridge.pool_active_mill_refs()
    released = pool.expire_stale_intents(active)
    for intent in released:
        notify.send_pool_dm(
            int(intent["telegram_id"]),
            "That order never fired — it was replaced, expired, or the setup "
            f"passed. Your ${float(intent['risk_usd']):,.2f} is back in your "
            "available balance. Nothing was risked.",
        )

    _deposit_sweep()
    _testwallet_deposit_sweep()
    _wallet_verify_sweep()
    _payout_sweep()
    _settle_sweep()
    # Kalshi autopilot runs on its own fast job (main.kalshi_autopilot_job),
    # not here — on this 60s scan it saw entries up to a minute late and the
    # slip gate refused what the house had just filled.
    _kalshi_settle_sweep()
    _treasury_confirm_sweep()
    _treasury_deploy_retry_sweep()

    global _pool_last_recon
    if config.EXECUTION_MODE != "live":
        return
    now = _time.time()
    if now - _pool_last_recon < _POOL_RECON_INTERVAL_SEC:
        return
    _pool_last_recon = now
    import treasury

    was_frozen = bool(pool.intents_frozen())
    # All locations client money can sit: Coinbase whole-account cash, the
    # test wallet on-chain, the Kalshi account, and transfers in flight.
    # One unreadable configured leg refuses the whole total — freezing the
    # pool on a transient API hiccup is the one outcome here that is worse
    # than checking late, and so is judging claims against a sum with a hole.
    total_info = treasury.reconcile_total()
    if not total_info.get("ok"):
        logger.error(
            "pool reconcile: %s (%s) — check skipped",
            total_info.get("reason"), total_info.get("detail"),
        )
        return

    snapshot = pool.reconcile(
        float(total_info["total_usd"]),
        breakdown=dict(total_info.get("breakdown") or {}),
    )
    if not snapshot["ok"] and not was_frozen:
        legs = " + ".join(
            f"{k.removesuffix('_usd')} ${v:,.2f}"
            for k, v in (snapshot.get("breakdown") or {}).items()
        )
        notify.send_pool_admin_alert(
            "POOL RECONCILE FAILED — new intents frozen.\n"
            f"Assets ${snapshot['venue_assets_usd']:,.2f} ({legs}) vs tester "
            f"claims ${snapshot['tester_cash_usd']:,.2f}.\n"
            "Audit pool_events / treasury_transfers against the venues, then "
            "unfreeze via pool.unfreeze_intents()."
        )


def run_watchdog() -> list[Suggestion] | None:
    """Run one watchdog scan; fire at most once per configured product."""
    if not bot_config.WATCHDOG_ENABLED:
        return None

    from macro.context import decision_macro_snapshot

    spots = research.get_spot_prices()
    try:
        user_books.expire_pending_decisions()
        user_books.check_user_sl_tp(spots=spots)
        notify.process_missed_connections(spots=spots)
    except Exception:
        logger.exception("Watchdog user-book maintenance failed")

    # Retire mill cards nobody acted on, so silence is a pass and a late
    # Accept can't fill a setup whose premise has gone.
    try:
        import trade_ideas_bridge

        trade_ideas_bridge.expire_stale_ideas()
    except Exception:
        logger.exception("Watchdog idea expiry failed")

    posture = active_posture()
    macro_snap = decision_macro_snapshot(posture)
    execute = bot_config.watchdog_execute_enabled()

    # Send any plan whose entry price has arrived. This runs on the scan
    # cadence rather than the trade cycle's because the fills that matter land
    # within minutes of the plan being written; waiting for the next boundary
    # would miss most of them. ``spots`` above is already fresh, so the check
    # costs nothing extra.
    try:
        import live_pending

        live_pending.sweep(spots)
    except Exception:
        logger.exception("Live pending entry sweep failed")

    # Reconcile exchange-side closes (stop fills / manual flattens) each scan.
    try:
        import execute as live_exec

        live_exec.sync_live_positions()
    except Exception:
        logger.exception("Live position sync failed")

    # Tester pool upkeep: release intents whose order can no longer fire, and
    # verify the venue still covers every tester's claim.
    if bot_config.POOL_ENABLED:
        try:
            _pool_sweep(spots)
        except Exception:
            logger.exception("Pool sweep failed")

    try:
        import case_study

        case_study.backfill_missing(limit=1)
    except Exception:
        logger.exception("Case study backfill failed")

    # Keeping a clip open is the mill's whole objective, and closing one is not
    # the only way the sleeve empties (a flatten, or a close while the service
    # was down). Cheap when occupied: the sweep is skipped without a DB read.
    try:
        import execute as live_exec
        import trade_ideas_bridge

        if live_exec.mill_capacity()["open"] == 0:
            trade_ideas_bridge.sweep_reoffer()
    except Exception:
        logger.exception("Mill sleeve refill check failed")
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
            skip_tags: list[str] = []
            will_execute = execute
            if trigger.direction == "bearish" and not bot_config.WATCHDOG_ALLOW_SHORTS:
                logger.info(
                    "Watchdog: shorts disabled — shadowing %s %s",
                    product_id,
                    trigger.name,
                )
                skip_tags.append("watchdog_shorts_disabled")
                will_execute = False
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
                skip_tags.append("relative_strength_gate")
                continue
            if posture.get("gate_long") and trigger.direction == "bullish":
                skip_tags.append("macro_gate_long")
                continue
            if posture.get("gate_short") and trigger.direction == "bearish":
                skip_tags.append("macro_gate_short")
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
            fire_tags = list(ctx.setup_tags) + skip_tags
            if not will_execute:
                fire_tags.append("watchdog_shadow")
            setup_tags = ",".join(fire_tags) if fire_tags else None
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
                executed=will_execute,
                trigger_name=trigger.name,
                macro_json=macro_snap,
                reason_code="trade" if will_execute else "watchdog_shadow",
            )

            if will_execute:
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
                    logger.exception(
                        "Display summary generation failed for %s", cycle_id
                    )
                    card_summary = None
                import vault as hq_vault

                hq_vault.take(
                    suggestion,
                    cycle_id=cycle_id,
                    spot=spots.get(product_id, live_spot),
                    title=display_summary.friendly_title(suggestion),
                    blurb=card_summary,
                )
                # Watchdog live fills need BOTH EXECUTION_MODE=shadow|live and
                # the separate watchdog-live gate (paper execute is not enough).
                if bot_config.watchdog_live_enabled():
                    import execute as live_exec

                    live_exec.maybe_execute_live(
                        suggestion,
                        spots.get(product_id, live_spot),
                        cycle_id=cycle_id,
                        source="hq",
                    )
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
                "Watchdog %s: cycle=%s product=%s trigger=%s "
                "action=%s entry=%s charts=%s",
                "trade fired" if will_execute else "shadow logged",
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
