"""/brain — Eva's consolidated current read, in one reply.

Assembles artifacts the system already produces (nothing here runs a new
model call, so the command is fast and free):

  1. Vision charts — marked H4 / H1 / M15 structure boards for BTC and ETH
     (order blocks and key levels, not a buy/sell Decision card).
  2. The conditional ICT read (intelligence/conditional.py -> intel_reads):
     order blocks, breakers, SFPs by name, with while-this-holds / drawing-to
     / invalidation.
  3. Four-year cycle overview (phase, pivots, written thesis).
  4. Live news (classified macro events with bias %, severity, read, link).

Every section degrades to a one-liner rather than failing the command — the
read is assembled from independent stores and any of them can be empty
on a fresh box.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_NEWS = 4
_VISION_PRODUCTS = ("BTC-USD", "ETH-USD")
_VISION_TFS = ("H4", "H1", "M15")

# Charts + ICT refresh with the trading / stance cycle.
REFRESH_NOTE = (
    "Charts and the ICT read refresh with every cycle (about every 30 minutes)."
)


def _fmt_zone(lo: Any, hi: Any) -> str:
    try:
        lo_f, hi_f = float(lo), float(hi)
    except (TypeError, ValueError):
        return "?"
    if abs(lo_f - hi_f) < 1e-9:
        return f"${lo_f:,.0f}"
    return f"${lo_f:,.0f}–${hi_f:,.0f}"


def _kind_label(kind: Any) -> str:
    return str(kind or "").replace("_", " ").strip() or "level"


def _read_line(read: dict[str, Any]) -> str:
    """One conditional read as 'if price goes here, expect that' copy."""
    product = str(read.get("product_id") or "?").replace("-USD", "")
    tf = str(read.get("timeframe") or "?")
    bias = (read.get("bias") or "no call").replace("_", " ")
    parts = [f"{product} {tf}: {bias}"]

    rep_kind = read.get("repelling_kind")
    if rep_kind:
        kind = _kind_label(rep_kind)
        zone = _fmt_zone(read.get("repelling_lo"), read.get("repelling_hi"))
        side = str(read.get("repelling_side") or "").replace("_", " ")
        state = str(read.get("repelling_state") or "").replace("_", " ")
        desc = f"holding {side} {kind} at {zone}" if side else f"{kind} at {zone}"
        if state:
            desc += f" ({state})"
        parts.append(desc)

    att_kind = read.get("attracting_kind")
    if att_kind:
        kind = _kind_label(att_kind)
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

    dropped = str(read.get("dropped_reason") or "").strip()
    if dropped:
        parts.append(f"dropped: {dropped}")

    line = " — ".join(parts)
    rationale = str(read.get("rationale") or "").strip()
    if rationale:
        line += f"\n  {rationale}"
    return "• " + line


def vision_chart_paths() -> list[str]:
    """Marked stance-board PNGs (BTC/ETH × H4/H1/M15), order preserved."""
    try:
        from dashboard.charts import stance_chart_path
    except Exception:  # noqa: BLE001
        logger.exception("brain: stance chart resolver unavailable")
        return []
    paths: list[str] = []
    for product_id in _VISION_PRODUCTS:
        for tf in _VISION_TFS:
            path = stance_chart_path(product_id, tf)
            if path is not None and path.is_file():
                paths.append(str(path))
    return paths


def vision_charts_caption() -> str:
    return (
        "Vision across the timeframes Eva is monitoring — H4 / H1 / M15 for "
        "BTC and ETH, with relevant order blocks and key levels marked.\n"
        "No buy/sell recommendation on these boards.\n\n"
        + REFRESH_NOTE
    )


def cycle_chart_path() -> str | None:
    """PNG snapshot of the BTC four-year cycle chart, if on disk."""
    try:
        from intelligence import store

        row = store.latest_long_thesis()
    except Exception:  # noqa: BLE001
        logger.exception("brain: cycle chart unavailable")
        return None
    if not row:
        return None
    raw = row.get("chart_path")
    if not raw:
        return None
    path = Path(str(raw))
    return str(path) if path.is_file() else None


def ict_view_text() -> str:
    """The conditional ICT read — OBs, breakers, SFPs, invalidations."""
    try:
        from intelligence import store

        reads = list(store.latest_reads())
    except Exception:  # noqa: BLE001
        logger.exception("brain: conditional reads unavailable")
        reads = []

    active = [r for r in reads if not r.get("dropped_reason")]
    dropped = [r for r in reads if r.get("dropped_reason")]

    if not reads:
        return (
            "ICT view: no conditional read this cycle — detectors found no "
            "holding order block / breaker / SFP array to name, or the next "
            "cycle has not written one yet.\n\n"
            + REFRESH_NOTE
        )

    lines = [
        "ICT view — order blocks, breakers, SFPs, invalidations",
        "",
    ]
    if active:
        for read in active[:12]:
            lines.append(_read_line(read))
    else:
        lines.append(
            "No active holding array right now (every candidate was dropped "
            "or marked no-call)."
        )
        for read in dropped[:6]:
            lines.append(_read_line(read))

    lines += ["", REFRESH_NOTE]
    return "\n".join(lines)


def cycle_text() -> str:
    """Four-year cycle position, pivot timing, and the written thesis."""
    try:
        from intelligence import cycle_phases, store

        phase, days_since = cycle_phases.current_phase()
        label = cycle_phases.PHASE_LABELS.get(phase, phase.replace("_", " "))
        lines = [
            f"Four-year cycle: {label} — day {days_since} since the last "
            "halving."
        ]

        thesis_row = store.latest_long_thesis()
        pos: dict[str, Any] = {}
        if thesis_row:
            thesis = thesis_row.get("thesis") or {}
            pos = thesis.get("cycle_position") or {}
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

            # Headline metrics from the dashboard cycle strip.
            months = pos.get("months_since_halving")
            progress = pos.get("cycle_progress_pct")
            dd = pos.get("drawdown_from_ath_pct")
            bits: list[str] = []
            if months is not None and days_since is not None:
                bits.append(f"{months}m · {days_since}d since halving")
            elif days_since is not None:
                bits.append(f"day {days_since} since halving")
            if progress is not None:
                bits.append(f"cycle {progress}%")
            if dd is not None:
                bits.append(f"from ATH {dd:+.2f}%")
            if bits:
                lines.append(" · ".join(bits))

            pivot = pos.get("next_projected_pivot") or {}
            days_to = pos.get("days_to_next_pivot")
            if pivot:
                kind = str(pivot.get("kind") or "pivot")
                date = str(pivot.get("date") or "")
                pivot_line = f"Next projected pivot: {kind}"
                if date:
                    pivot_line += f" · {date}"
                if days_to is not None:
                    pivot_line += f" ({days_to}d)"
                lines.append(pivot_line)
                if str(kind).lower() == "low":
                    lines.append(
                        "Eva's read on the bottom: a projected cycle low in "
                        f"about {days_to} day(s)"
                        if days_to is not None
                        else "Eva's read on the bottom: next projected pivot "
                        "is a cycle low."
                    )

            if summary:
                lines.append(summary[:500])

        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        logger.exception("brain: cycle overview unavailable")
        return "Four-year cycle: read unavailable right now."


def news_text() -> str:
    """Biggest live headlines with bias %, SEV 1–5, read, and link."""
    try:
        from macro import store as macro_store
        from macro.bias_score import best_bias

        events = macro_store.get_active_events(min_severity=3)[:_MAX_NEWS]
        if not events:
            events = macro_store.get_active_events(min_severity=1)[:_MAX_NEWS]
    except Exception:  # noqa: BLE001
        logger.exception("brain: macro events unavailable")
        events = []
    if not events:
        return (
            "News: nothing severe is live right now — no active headline is "
            "gating entries."
        )
    lines = [
        "Biggest news right now:",
        "(Severity is on a scale of 1 to 5.)",
        "",
    ]
    for e in events:
        try:
            bias = best_bias(e)
        except Exception:  # noqa: BLE001
            bias = {
                "side": e.get("eth_bias") or e.get("bias_side"),
                "pct": e.get("bias_pct"),
                "one_liner": e.get("eth_impact_summary"),
            }
        side = str(bias.get("side") or "").replace("_", " ").strip()
        pct = bias.get("pct")
        sev = e.get("severity")
        title = str(e.get("title") or e.get("headline") or "").strip()[:160]
        url = str(e.get("url") or "").strip()
        read = str(
            bias.get("one_liner")
            or e.get("bias_one_liner")
            or e.get("eth_impact_summary")
            or ""
        ).strip()

        tag_bits: list[str] = []
        if side and pct is not None:
            tag_bits.append(f"{side} {int(pct)}%")
        elif side:
            tag_bits.append(side)
        if sev is not None:
            tag_bits.append(f"SEV {sev}/5")
        tag = f"[{', '.join(tag_bits)}]" if tag_bits else ""

        lines.append(f"• {tag} {title}".strip())
        if read:
            lines.append(f"  Read: {read[:240]}")
        if url:
            lines.append(f"  {url}")
    return "\n".join(lines)


def synthesize_read() -> str:
    """One synthesized view across ICT, four-year cycle, and news/macro."""
    ict = ict_view_text()
    # Drop the trailing refresh note from subsections; one note at the end.
    ict_body = ict.replace(REFRESH_NOTE, "").strip()
    cycle = cycle_text().strip()
    news = news_text().strip()

    ict_brief = _brief_ict(ict_body)
    cycle_brief = _brief_cycle(cycle)
    news_brief = _brief_news(news)

    synthesis = (
        f"Synthesized read: {cycle_brief} Near-term ICT: {ict_brief} "
        f"Macro tape: {news_brief}"
    )

    sections = [
        "Eva's brain — Today's Read",
        "",
        synthesis,
        "",
        "— ICT —",
        ict_body,
        "",
        "— Four-year cycle —",
        cycle,
        "",
        "— News / macro —",
        news,
        "",
        REFRESH_NOTE,
        "This read refreshes about every 30 minutes.",
    ]
    return "\n".join(sections)


def _brief_ict(ict_body: str) -> str:
    if "no conditional read" in ict_body.lower():
        return "no holding OB/breaker/SFP array named this cycle."
    if "No active holding" in ict_body:
        return "no active holding array (candidates dropped or no-call)."
    # First bullet after the header.
    for line in ict_body.splitlines():
        if line.startswith("• "):
            return line[2:].split("\n")[0][:220]
    return "see ICT section below."


def _brief_cycle(cycle: str) -> str:
    lines = [ln.strip() for ln in cycle.splitlines() if ln.strip()]
    if not lines:
        return "cycle read unavailable."
    head = lines[0]
    pivot = next((ln for ln in lines if ln.lower().startswith("next projected")), "")
    thesis = next((ln for ln in lines if ln.lower().startswith("thesis:")), "")
    bits = [head]
    if thesis:
        bits.append(thesis)
    if pivot:
        bits.append(pivot)
    return " ".join(bits)


def _brief_news(news: str) -> str:
    if news.lower().startswith("news: nothing"):
        return "no severe headline gating entries."
    for line in news.splitlines():
        if line.startswith("• "):
            return line[2:][:200]
    return "see news section below."


def build_report() -> dict[str, Any]:
    """Everything /brain and Today's Read send: synthesis + vision charts."""
    text = synthesize_read()
    paths = vision_chart_paths()
    return {
        "text": text,
        "chart_paths": paths,
        "caption": vision_charts_caption() if paths else None,
        # Legacy key — callers that still expect `view` get vision charts,
        # never a Decision trade card.
        "view": None,
    }
