"""Assemble deterministic market signals for the vision agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from patterns.htf_structure import HTFZone, detect_htf_zones
from patterns.key_levels import KeyLevel, compute_key_levels, nearest_levels
from patterns.order_block import OrderBlock, find_order_blocks, price_in_ob
from patterns.range_24h import Range24h, compute_range_24h, detect_range_break
from patterns.setup_state import SetupState, update_bearish_retest_state
from patterns.sfp import SFPEvent, detect_sfps
from patterns.signal_state import get_state, set_state
from patterns.zone_resolver import ZoneSnapshot, format_zone, resolve_zones

RANGE_STATE_KEY = "range_24h_announced"
SFP_MAX_AGE_HOURS = 18
H1_SFP_MAX_BARS = 18


@dataclass
class MarketContext:
    range_24h: Range24h | None
    is_ranging: bool
    range_break: str | None
    spot: float
    zone_snapshot: ZoneSnapshot | None
    setup_state: SetupState | None
    alerts: list[str] = field(default_factory=list)
    h12_sfps: list[SFPEvent] = field(default_factory=list)
    h1_sfps: list[SFPEvent] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    htf_zones: list[HTFZone] = field(default_factory=list)
    key_levels_near: list[KeyLevel] = field(default_factory=list)
    setup_tags: list[str] = field(default_factory=list)
    summary_text: str = ""

    def to_prompt_block(self) -> str:
        return self.summary_text


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _is_sfp_live_valid(event: SFPEvent, spot: float) -> bool:
    """False when spot has closed back through the swept level (post-window invalidation)."""
    if event.direction == "bullish":
        return spot >= event.swept_level
    return spot <= event.swept_level


def _filter_recent_sfps(
    events: list[SFPEvent],
    *,
    spot: float | None = None,
    max_age_hours: int = SFP_MAX_AGE_HOURS,
    max_bars: int | None = None,
    now: datetime | None = None,
) -> tuple[list[SFPEvent], list[SFPEvent]]:
    """Keep recent reversal/pending SFPs; return (valid, live_invalidated)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    recent: list[SFPEvent] = []
    live_invalidated: list[SFPEvent] = []
    for event in events:
        if event.outcome_a not in ("reversal", "pending"):
            continue
        try:
            event_ts = _parse_ts(event.ts)
        except ValueError:
            continue
        if event_ts < cutoff:
            continue
        if spot is not None and not _is_sfp_live_valid(event, spot):
            live_invalidated.append(event)
            continue
        recent.append(event)
    if max_bars is not None and len(recent) > max_bars:
        recent = recent[-max_bars:]
    return recent[-3:], live_invalidated


def _bars_since_extreme(h1_bars: list[dict], field_name: str) -> tuple[int, float] | None:
    """How many H1 bars ago the rolling 24h high/low was made."""
    if len(h1_bars) < 24:
        return None
    window = h1_bars[-24:]
    if field_name == "high":
        extreme = max(float(b["high"]) for b in window)
        matches = [i for i, b in enumerate(window) if float(b["high"]) == extreme]
    else:
        extreme = min(float(b["low"]) for b in window)
        matches = [i for i, b in enumerate(window) if float(b["low"]) == extreme]
    if not matches:
        return None
    bars_ago = (len(window) - 1) - matches[-1]
    return bars_ago, extreme


def _htf_bearish_bias(h12_bars: list[dict], zone_snap: ZoneSnapshot) -> bool:
    if len(h12_bars) < 20:
        return zone_snap.primary_bearish is not None
    closes = [float(b["close"]) for b in h12_bars[-20:]]
    mid = len(closes) // 2
    first_avg = sum(closes[:mid]) / mid
    second_avg = sum(closes[mid:]) / (len(closes) - mid)
    trending_down = second_avg < first_avg * 0.995
    return trending_down or (
        zone_snap.primary_bearish is not None
        and zone_snap.primary_bullish is None
    )


def _format_ob(ob: OrderBlock) -> str:
    return (
        f"{ob.direction} OB {ob.low:,.2f}-{ob.high:,.2f} "
        f"(displacement {ob.displacement_ts[:16]})"
    )


def _format_sfp(event: SFPEvent) -> str:
    age_h = ""
    try:
        delta = datetime.now(timezone.utc) - _parse_ts(event.ts)
        age_h = f", {delta.total_seconds() / 3600:.0f}h ago"
    except ValueError:
        pass
    return (
        f"{event.ts[:16]} {event.direction} SFP @ {event.swept_level:,.2f} "
        f"-> {event.outcome_a}{age_h}"
    )


def build_market_context(
    h12_bars: list[dict],
    h4_bars: list[dict],
    h1_bars: list[dict],
    daily_bars: list[dict] | None = None,
) -> MarketContext:
    """Compute ICT signals and range alerts from live OHLC."""
    alerts: list[str] = []
    setup_tags: list[str] = []
    spot = float(h1_bars[-1]["close"]) if h1_bars else 0.0

    range_24h = compute_range_24h(h1_bars)
    is_ranging = bool(range_24h and range_24h.is_ranging)
    range_break: str | None = None

    if range_24h:
        prev = get_state(RANGE_STATE_KEY)
        if prev is None:
            alerts.append(
                f"24h range established: {range_24h.low:,.2f} - {range_24h.high:,.2f} "
                f"(width {range_24h.width_pct:.1f}%)"
            )
            setup_tags.append("range_24h_new")
        else:
            prev_high = float(prev["high"])
            prev_low = float(prev["low"])
            range_break = detect_range_break(spot, prev_high, prev_low)
            if range_break == "above":
                alerts.append(
                    f"24h range BREAK ABOVE {prev_high:,.2f} "
                    f"(prior range {prev_low:,.2f}-{prev_high:,.2f})"
                )
                setup_tags.append("range_24h_break_above")
            elif range_break == "below":
                alerts.append(
                    f"24h range BREAK BELOW {prev_low:,.2f} "
                    f"(prior range {prev_low:,.2f}-{prev_high:,.2f})"
                )
                setup_tags.append("range_24h_break_below")
            elif range_24h.high >= prev_high * 1.002 and range_24h.high > prev_high:
                alerts.append(
                    f"24h high expanded to {range_24h.high:,.2f} "
                    f"(prior high {prev_high:,.2f}) — upside retest in progress"
                )
                setup_tags.append("range_high_expanded")
            elif (
                abs(range_24h.high - prev_high) / prev_high > 0.005
                or abs(range_24h.low - prev_low) / prev_low > 0.005
            ):
                alerts.append(
                    f"24h range updated: {range_24h.low:,.2f} - {range_24h.high:,.2f}"
                )

        set_state(
            RANGE_STATE_KEY,
            {"high": range_24h.high, "low": range_24h.low, "end_ts": range_24h.end_ts},
        )

        if is_ranging:
            setup_tags.append("ranging")

    h12_sfps = detect_sfps(h12_bars, timeframe="H12")
    h1_sfps = detect_sfps(h1_bars, timeframe="H1")
    recent_h12, inv_h12 = _filter_recent_sfps(h12_sfps, spot=spot, max_age_hours=36)
    recent_h1, inv_h1 = _filter_recent_sfps(
        h1_sfps,
        spot=spot,
        max_age_hours=SFP_MAX_AGE_HOURS,
        max_bars=H1_SFP_MAX_BARS,
    )
    live_invalidated = inv_h12 + inv_h1

    for event in recent_h12:
        setup_tags.append(f"h12_sfp_{event.direction}")
    for event in recent_h1:
        setup_tags.append(f"h1_sfp_{event.direction}")

    htf_zones = detect_htf_zones(h12_bars)
    zone_snap = resolve_zones(spot, htf_zones)
    bearish_bias = _htf_bearish_bias(h12_bars, zone_snap)

    recent_bearish_h1 = any(e.direction == "bearish" for e in recent_h1)
    setup_state, setup_alerts, setup_state_tags = update_bearish_retest_state(
        spot=spot,
        range_high_24h=range_24h.high if range_24h else None,
        retest_low=zone_snap.bearish_retest_low,
        retest_high=zone_snap.bearish_retest_high,
        htf_bearish_bias=bearish_bias,
        recent_bearish_h1_sfp=recent_bearish_h1,
    )
    alerts.extend(setup_alerts)
    setup_tags.extend(setup_state_tags)

    # Canonical zone alerts (replace noisy multi-OB dumps)
    if zone_snap.primary_bearish and zone_snap.primary_bullish:
        alerts.append(
            "STRUCTURE CONFLICT: price inside both bullish and bearish H12 zones — "
            "require clear LTF+HTF alignment; do not cite only the bullish OB"
        )
        setup_tags.append("htf_zone_conflict")
    elif zone_snap.primary_bearish:
        low, high = zone_snap.bearish_retest_low, zone_snap.bearish_retest_high
        alerts.append(
            f"Primary H12 zone: BEARISH {zone_snap.primary_bearish.low:,.2f}-"
            f"{zone_snap.primary_bearish.high:,.2f} | supply retest {low:,.2f}-{high:,.2f}"
        )
    elif zone_snap.primary_bullish:
        alerts.append(
            f"Primary H12 zone: BULLISH {zone_snap.primary_bullish.low:,.2f}-"
            f"{zone_snap.primary_bullish.high:,.2f}"
        )

    if (
        range_24h
        and zone_snap.bearish_retest_low is not None
        and range_24h.high >= zone_snap.bearish_retest_low
        and spot < zone_snap.bearish_retest_low
    ):
        alerts.append(
            "Do NOT say 'waiting for rally into retest zone' — 24h high already tagged supply; "
            "evaluate SHORT on rejection"
        )
        setup_tags.append("retest_already_tagged")

    order_blocks = find_order_blocks(h1_bars)
    for ob in order_blocks:
        if price_in_ob(spot, ob):
            side = "short" if ob.direction == "bearish" else "long"
            alerts.append(
                f"Price inside {ob.direction} H1 OB ({ob.low:,.2f}-{ob.high:,.2f}) "
                f"- potential {side} setup"
            )
            setup_tags.append(f"h1_ob_{ob.direction}_in_zone")

    key_levels_near: list[KeyLevel] = []
    if daily_bars:
        all_levels = compute_key_levels(daily_bars)
        key_levels_near = nearest_levels(all_levels, spot, n=4)

    high_age = _bars_since_extreme(h1_bars, "high")
    low_age = _bars_since_extreme(h1_bars, "low")

    lines = [
        "=== Programmatic market context (verify against charts) ===",
        f"Current spot: ${spot:,.2f}",
    ]

    if range_24h:
        dist_below_high = range_24h.high - spot
        dist_above_low = spot - range_24h.low
        lines.append(
            f"24h range: {range_24h.low:,.2f} - {range_24h.high:,.2f} "
            f"| ranging={is_ranging} | width={range_24h.width_pct:.1f}%"
        )
        lines.append(
            f"Distance from spot: {dist_below_high:,.2f} below 24h high, "
            f"{dist_above_low:,.2f} above 24h low"
        )
        if high_age is not None:
            bars_ago, extreme = high_age
            lines.append(
                f"24h high {extreme:,.2f} was made {bars_ago} H1 bar(s) ago "
                f"({bars_ago}h)"
            )
        if low_age is not None:
            bars_ago, extreme = low_age
            lines.append(
                f"24h low {extreme:,.2f} was made {bars_ago} H1 bar(s) ago "
                f"({bars_ago}h)"
            )
    else:
        lines.append("24h range: insufficient H1 data")

    if setup_state and setup_state.phase != "idle":
        lines.append(
            f"Setup state: {setup_state.phase}"
            + (
                f" | retest zone {setup_state.retest_low:,.2f}-"
                f"{setup_state.retest_high:,.2f}"
                if setup_state.retest_low is not None
                else ""
            )
            + (
                f" | tagged high {setup_state.tagged_high:,.2f}"
                if setup_state.tagged_high is not None
                else ""
            )
        )

    if zone_snap.zones_containing_price:
        lines.append("Canonical H12 zones at price:")
        for z in zone_snap.zones_containing_price:
            lines.append(f"  - {format_zone(z)}")

    if zone_snap.bearish_retest_low is not None:
        lines.append(
            f"Bearish supply retest zone: {zone_snap.bearish_retest_low:,.2f}-"
            f"{zone_snap.bearish_retest_high:,.2f}"
        )
        if range_24h and range_24h.high >= zone_snap.bearish_retest_low:
            lines.append(
                "RETEST STATUS: FILLED (24h high reached supply) — "
                "do not describe this as a future rally"
            )
        else:
            lines.append("RETEST STATUS: NOT YET FILLED")

    if alerts:
        lines.append("Alerts:")
        lines.extend(f"  - {a}" for a in alerts)

    if recent_h12:
        lines.append(f"Recent H12 SFPs (last {SFP_MAX_AGE_HOURS}h window):")
        lines.extend(f"  - {_format_sfp(e)}" for e in recent_h12)
    else:
        lines.append("Recent H12 SFPs: none in time window")

    if recent_h1:
        lines.append(f"Recent H1 SFPs (last {SFP_MAX_AGE_HOURS}h):")
        lines.extend(f"  - {_format_sfp(e)}" for e in recent_h1)
    else:
        lines.append("Recent H1 SFPs: none in time window")

    if live_invalidated:
        lines.append("Live-invalidated SFPs (excluded — spot negated swept level):")
        for event in live_invalidated:
            reason = (
                f"spot {spot:,.2f} below swept {event.swept_level:,.2f}"
                if event.direction == "bullish"
                else f"spot {spot:,.2f} above swept {event.swept_level:,.2f}"
            )
            lines.append(f"  - {_format_sfp(event)} ({reason})")

    if key_levels_near:
        lines.append("Nearest key levels to spot:")
        for lv in key_levels_near:
            lines.append(f"  - {lv.label} @ {lv.price:,.2f}")

    lines.extend(
        [
            "",
            "Decision rules:",
            "- If RETEST STATUS is FILLED, do NOT say price has not reached the retest zone.",
            "- If setup state is bearish_retest_rejected or short_trigger_retest, strongly favor SHORT.",
            "- If HTF zone conflict, default no_trade unless LTF+HTF align clearly.",
            "- Only cite SFPs listed under Recent H12/H1 SFPs; do not cite Live-invalidated SFPs.",
            "- Trades: structure_chart=H12, entry_chart=H1 unless exceptional.",
            "",
            "Use marked charts plus this context. Mention 24h range and setup state in rationale.",
        ]
    )

    return MarketContext(
        range_24h=range_24h,
        is_ranging=is_ranging,
        range_break=range_break,
        spot=spot,
        zone_snapshot=zone_snap,
        setup_state=setup_state,
        alerts=alerts,
        h12_sfps=recent_h12,
        h1_sfps=recent_h1,
        order_blocks=order_blocks,
        htf_zones=htf_zones,
        key_levels_near=key_levels_near,
        setup_tags=list(dict.fromkeys(setup_tags)),
        summary_text="\n".join(lines),
    )
