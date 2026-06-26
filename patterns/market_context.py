"""Assemble deterministic market signals for the vision agent."""

from __future__ import annotations

from dataclasses import dataclass, field

from patterns.order_block import OrderBlock, find_order_blocks, price_in_ob
from patterns.range_24h import Range24h, compute_range_24h, detect_range_break
from patterns.signal_state import get_state, set_state
from patterns.sfp import SFPEvent, detect_sfps

RANGE_STATE_KEY = "range_24h_announced"


@dataclass
class MarketContext:
    range_24h: Range24h | None
    is_ranging: bool
    range_break: str | None
    alerts: list[str] = field(default_factory=list)
    h12_sfps: list[SFPEvent] = field(default_factory=list)
    h1_sfps: list[SFPEvent] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    setup_tags: list[str] = field(default_factory=list)
    summary_text: str = ""

    def to_prompt_block(self) -> str:
        return self.summary_text


def _format_ob(ob: OrderBlock) -> str:
    return (
        f"{ob.direction} OB {ob.low:,.2f}-{ob.high:,.2f} "
        f"(displacement {ob.displacement_ts[:16]})"
    )


def _format_sfp(event: SFPEvent) -> str:
    return (
        f"{event.ts[:16]} {event.direction} SFP @ {event.swept_level:,.2f} "
        f"-> {event.outcome_a}"
    )


def build_market_context(
    h12_bars: list[dict],
    h4_bars: list[dict],
    h1_bars: list[dict],
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
    recent_h12 = [e for e in h12_sfps if e.outcome_a in ("reversal", "pending")][-3:]
    recent_h1 = [e for e in h1_sfps if e.outcome_a in ("reversal", "pending")][-3:]

    for event in recent_h12:
        setup_tags.append(f"h12_sfp_{event.direction}")
    for event in recent_h1:
        setup_tags.append(f"h1_sfp_{event.direction}")

    order_blocks = find_order_blocks(h1_bars)
    h12_blocks = find_order_blocks(h12_bars, lookback=40)
    all_blocks = order_blocks + h12_blocks

    for ob in order_blocks:
        if price_in_ob(spot, ob):
            side = "short" if ob.direction == "bearish" else "long"
            alerts.append(
                f"Price inside {ob.direction} H1 OB ({ob.low:,.2f}-{ob.high:,.2f}) "
                f"- potential {side} setup"
            )
            setup_tags.append(f"h1_ob_{ob.direction}_in_zone")

    lines = [
        "=== Programmatic market context (verify against charts) ===",
    ]
    if range_24h:
        lines.append(
            f"24h range: {range_24h.low:,.2f} - {range_24h.high:,.2f} "
            f"| ranging={is_ranging} | width={range_24h.width_pct:.1f}%"
        )
    else:
        lines.append("24h range: insufficient H1 data")

    if alerts:
        lines.append("Alerts:")
        lines.extend(f"  - {a}" for a in alerts)

    if recent_h12:
        lines.append("Recent H12 SFPs:")
        lines.extend(f"  - {_format_sfp(e)}" for e in recent_h12)

    if recent_h1:
        lines.append("Recent H1 SFPs:")
        lines.extend(f"  - {_format_sfp(e)}" for e in recent_h1)

    if all_blocks:
        lines.append("Detected order blocks:")
        for ob in all_blocks[-4:]:
            lines.append(f"  - {_format_ob(ob)}")

    lines.append(
        "Use this context plus H12/H4/H1 charts. "
        "Confirm ranging, OB location, and entry direction. "
        "Mention 24h range and any breaks in rationale."
    )

    return MarketContext(
        range_24h=range_24h,
        is_ranging=is_ranging,
        range_break=range_break,
        alerts=alerts,
        h12_sfps=recent_h12,
        h1_sfps=recent_h1,
        order_blocks=all_blocks,
        setup_tags=list(dict.fromkeys(setup_tags)),
        summary_text="\n".join(lines),
    )
