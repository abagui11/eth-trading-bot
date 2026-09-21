"""/brain — Eva's consolidated current read, in one reply.

Assembles artifacts the system already produces (nothing here runs a new
model call, so the command is fast and free):

  1. The latest marked charts per timeframe — rendered by the trading cycle
     every half-hour cadence, resolved through chart_view.
  2. The conditional ICT read (intelligence/conditional.py -> intel_reads):
     order blocks and breakers by name, with the "while this holds / if it
     breaks" logic and an explicit invalidation price.
  3. A quick four-year cycle overview (phase + written thesis).
  4. The biggest live news right now (classified macro events).

Every section degrades to a one-liner rather than failing the command — the
read is assembled from four independent stores and any of them can be empty
on a fresh box.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_NEWS = 3

# The read (charts + conditional ICT view) refreshes with the trading cycle.
_REFRESH_NOTE = "Charts and the ICT read refresh with every cycle (about every 30 minutes)."


def _fmt_zone(lo: Any, hi: Any) -> str:
    try:
        lo_f, hi_f = float(lo), float(hi)
    except (TypeError, ValueError):
        return "?"
    if abs(lo_f - hi_f) < 1e-9:
        return f"${lo_f:,.0f}"
    return f"${lo_f:,.0f}–${hi_f:,.0f}"


def _read_line(read: dict[str, Any]) -> str:
    """One conditional read as 'if price goes here, expect that' copy."""
    product = str(read.get("product_id") or "?").replace("-USD", "")
    tf = str(read.get("timeframe") or "?")
    bias = (read.get("bias") or "no bias").replace("_", " ")
    parts = [f"{product} {tf}: {bias}"]

    rep_kind = read.get("repelling_kind")
    if rep_kind:
        kind = str(rep_kind).replace("_", " ")
        zone = _fmt_zone(read.get("repelling_lo"), read.get("repelling_hi"))
        side = str(read.get("repelling_side") or "").replace("_", " ")
        state = str(read.get("repelling_state") or "").replace("_", " ")
        desc = f"holding {side} {kind} at {zone}" if side else f"{kind} at {zone}"
        if state:
            desc += f" ({state})"
        parts.append(desc)

    att_kind = read.get("attracting_kind")
    if att_kind:
        kind = str(att_kind).replace("_", " ")
        zone = _fmt_zone(read.get("attracting_lo"), read.get("attracting_hi"))
        parts.append(f"drawing toward the {kind} at {zone}")

    inv = read.get("invalidation_price")
    if inv is not None:
        try:
            trigger = str(read.get("invalidation_trigger") or "a close through")
            parts.append(
                f"invalidated on {trigger.replace('_', ' ')} ${float(inv):,.0f}"
            )
        except (TypeError, ValueError):
            pass

    line = " — ".join(parts)
    rationale = str(read.get("rationale") or "").strip()
    if rationale:
        line += f"\n  {rationale}"
    return "• " + line


def ict_view_text() -> str:
    """The conditional ICT read as a compact monospace table."""
    try:
        from intelligence import store

        reads = [
            r for r in store.latest_reads()
            if not r.get("dropped_reason")
        ]
    except Exception:  # noqa: BLE001
        logger.exception("brain: conditional reads unavailable")
        reads = []
    if not reads:
        return (
            "ICT view: no conditional read available yet — the next cycle "
            "writes one."
        )
    lines = [
        "ICT view — order blocks, breakers, invalidations",
        "",
        f"{'Asset':<6} {'TF':<4} {'Bias':<10} Zone / invalidation",
        "-" * 48,
    ]
    for read in reads[:12]:
        product = str(read.get("product_id") or "?").replace("-USD", "")[:5]
        tf = str(read.get("timeframe") or "?")[:4]
        bias = (read.get("bias") or "—").replace("_", " ")[:10]
        zone = ""
        if read.get("repelling_kind"):
            zone = _fmt_zone(read.get("repelling_lo"), read.get("repelling_hi"))
        elif read.get("attracting_kind"):
            zone = _fmt_zone(read.get("attracting_lo"), read.get("attracting_hi"))
        inv = read.get("invalidation_price")
        inv_s = ""
        if inv is not None:
            try:
                inv_s = f" inv ${float(inv):,.0f}"
            except (TypeError, ValueError):
                pass
        lines.append(f"{product:<6} {tf:<4} {bias:<10} {zone}{inv_s}")
    return "\n".join(lines)


def build_report() -> dict[str, Any]:
    """Everything /brain sends: chart view (or None) + the text body."""
    import chart_view

    view = None
    try:
        view = chart_view.get_latest_chart_view()
    except Exception:  # noqa: BLE001
        logger.exception("brain: chart view unavailable")

    sections = [
        "Eva's brain — the read as it stands.",
        "",
    ]
    if view is not None and view.watch_summary:
        summary = str(view.watch_summary).strip()
        if len(summary) > 400:
            summary = summary[:400].rstrip() + "…"
        sections.append(summary)
        sections.append("")
    sections += [
        ict_view_text(),
        "",
        cycle_text(),
        "",
        news_text(),
        "",
        _REFRESH_NOTE,
    ]
    return {"text": "\n".join(sections), "view": view}


def cycle_text() -> str:
    """Four-year cycle position + the written thesis, in three lines."""
    try:
        from intelligence import cycle_phases, store

        phase, days_since = cycle_phases.current_phase()
        label = cycle_phases.PHASE_LABELS.get(phase, phase.replace("_", " "))
        lines = [
            f"Four-year cycle: {label} — day {days_since} since the last "
            "halving."
        ]
        thesis_row = store.latest_long_thesis()
        if thesis_row:
            thesis = thesis_row.get("thesis") or {}
            bias = str(thesis.get("bias") or "").strip()
            conf = thesis.get("confidence")
            summary = str(
                thesis.get("summary") or thesis.get("thesis") or ""
            ).strip()
            if bias:
                head = f"Thesis: {bias}"
                if conf is not None:
                    try:
                        head += f" (confidence {float(conf):.0%})"
                    except (TypeError, ValueError):
                        pass
                lines.append(head)
            if summary:
                lines.append(summary[:400])
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        logger.exception("brain: cycle overview unavailable")
        return "Four-year cycle: read unavailable right now."


def news_text() -> str:
    """The biggest live headlines, by severity."""
    try:
        from macro import store as macro_store

        events = macro_store.get_active_events(min_severity=3)[:_MAX_NEWS]
    except Exception:  # noqa: BLE001
        logger.exception("brain: macro events unavailable")
        events = []
    if not events:
        return (
            "News: nothing severe is live right now — no active headline is "
            "gating entries."
        )
    lines = ["Biggest news right now:"]
    for e in events:
        sev = e.get("severity")
        direction = str(e.get("direction") or "").replace("_", " ")
        title = str(e.get("title") or e.get("headline") or "").strip()[:160]
        tag = f"[sev {sev}"
        if direction:
            tag += f", {direction}"
        tag += "]"
        lines.append(f"• {tag} {title}")
    return "\n".join(lines)
