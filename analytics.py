"""Research analytics orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone

import charts
import ohlc_cache
import research
from patterns.invalidation_followup import (
    InvalidationFollowUp,
    compute_invalidation_stats,
    score_post_invalidation,
)
from patterns.sfp import SFPEvent, compute_stats, detect_sfps
from research_reports.format import ResearchReport

_METHODOLOGY: dict[str, str] = {
    "W1": (
        "Methodology: Coinbase ETH-USD, weekly W-FRI bars. "
        "SFP = L=3 pivot, wick sweeps >=0.2% past a swing from the last ~20 weeks, close back inside. "
        "Outcome A = >=2% follow-through from event close within N bars (or invalidation if close past level). "
        "B/C = >=5% move / structure break (same window)."
    ),
    "H12": (
        "Methodology: Coinbase ETH-USD, 12h bars resampled from H1. "
        "SFP = L=4 extreme pivot, latest swing swept >=0.3% within ~3 weeks, close back inside. "
        "Outcome A = >=1.5% follow-through from event close within N bars (or invalidation). "
        "B/C = >=5% move / structure break (same window)."
    ),
}

_LABELS: dict[str, str] = {
    "W1": "Weekly",
    "H12": "H12",
}

_TOPIC_IDS: dict[str, str] = {
    "W1": "weekly_sfp",
    "H12": "h12_sfp",
}


def _bar_count_note(timeframe: str, bar_count: int, years: int) -> str:
    per_year = round(bar_count / years, 1) if years else bar_count
    return f"Bars analyzed: {bar_count} (~{per_year}/year)"


def _build_sections(
    stats: dict,
    years: int,
    events: list[SFPEvent],
    timeframe: str,
    bar_count: int,
) -> list[tuple[str, list[str]]]:
    scored = stats["reversals"] + stats["invalidations"]
    metrics = [
        f"• {_bar_count_note(timeframe, bar_count, years)}",
        f"• Headline (Outcome A): {stats['reversal_pct']}% reversal",
        f"  {stats['reversals']} reversals / {stats['invalidations']} invalidations",
        f"  ({scored} scored; {stats['neutral']} neutral, {stats['pending']} pending)",
        f"• Total SFPs detected: {stats['total_sfps']}",
        f"• Outcome B (>=5% move): {stats['outcome_b_pct']}% "
        f"({stats['outcome_b_count']}/{stats['outcome_bc_eligible']})",
        f"• Outcome C (structure break): {stats['outcome_c_pct']}% "
        f"({stats['outcome_c_count']}/{stats['outcome_bc_eligible']})",
    ]
    recent_lines = ["Recent events:"]
    recent = sorted(events, key=lambda e: e.ts)[-5:]
    for e in recent:
        recent_lines.append(
            f"  {e.ts[:10]} {e.direction} @ {e.swept_level:,.0f} -> {e.outcome_a}"
        )
    methodology = _METHODOLOGY.get(timeframe, _METHODOLOGY["W1"])
    return [
        ("Metrics", metrics),
        ("Recent events", recent_lines[1:] if len(recent_lines) > 1 else ["• None"]),
        ("Methodology", [methodology]),
    ]


def _build_caption(stats: dict, years: int, timeframe: str) -> str:
    label = _LABELS.get(timeframe, timeframe)
    scored = stats["reversals"] + stats["invalidations"]
    return (
        f"{label} SFP — {years}y ETH-USD\n"
        f"{stats['reversal_pct']}% reversal ({stats['reversals']}/{scored} scored)\n"
        f"{stats['total_sfps']} SFPs detected"
    )[:1024]


def sfp_report(timeframe: str = "W1", years: int = 4) -> ResearchReport:
    """Run SFP study for W1 or H12: cache -> detect -> chart -> summary."""
    tf = timeframe.upper()
    if tf == "W1":
        bars = ohlc_cache.get_weekly_bars(years=years)
    elif tf == "H12":
        bars = ohlc_cache.get_h12_bars(years=years)
    else:
        raise ValueError(f"Unsupported research timeframe: {timeframe}")

    if not bars:
        raise RuntimeError(f"No {tf} bars available — run backfill.py first.")

    events = detect_sfps(bars, timeframe=tf)
    stats = compute_stats(events)

    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    chart_path = charts.render_research_chart(
        bars,
        events,
        stats,
        timeframe=tf,
        cycle_id=cycle_id,
        years=years,
    )

    label = _LABELS.get(tf, tf)
    scored = stats["reversals"] + stats["invalidations"]
    headline = (
        f"{label} SFP reversal study ({years} years): "
        f"{stats['reversal_pct']}% reversal ({stats['reversals']}/{scored} scored)"
    )

    interpretation = [
        f"{stats['total_sfps']} SFPs detected over {years} years on Coinbase ETH-USD.",
        "Outcome A measures reversal vs invalidation within the scoring window.",
        "Use as historical context — not a standalone trade signal.",
    ]

    return ResearchReport(
        topic=_TOPIC_IDS.get(tf, tf.lower()),
        title=f"{label} SFP Study",
        headline=headline,
        sections=_build_sections(stats, years, events, tf, len(bars)),
        interpretation=interpretation,
        sources=["Coinbase OHLC (ohlc.db)", "patterns/sfp.py"],
        chart_path=chart_path,
        caption=_build_caption(stats, years, tf),
    )


def weekly_sfp_report(years: int = 4) -> ResearchReport:
    """Run weekly SFP study: cache -> detect -> chart -> summary."""
    return sfp_report("W1", years=years)


def h12_sfp_report(years: int = 4) -> ResearchReport:
    """Run H12 SFP study: cache -> detect -> chart -> summary."""
    return sfp_report("H12", years=years)


_INVALIDATION_METHODOLOGY = (
    "Methodology: Coinbase ETH-USD H12 bars. "
    "Select SFPs where Outcome A = invalidation (close past swept level within N bars). "
    "Post-invalidation: continuation = >=1.5% move in invalidation direction from inv close; "
    "mean_reversion = >=1.5% fade back toward original SFP thesis (same N-bar window)."
)


def _format_followup_line(fu: InvalidationFollowUp) -> str:
    e = fu.event
    move = f", {fu.move_pct:.1f}% move" if fu.move_pct is not None else ""
    return (
        f"  {e.ts[:10]} {e.direction} @ {e.swept_level:,.0f} "
        f"-> post-inv: {fu.outcome}{move}"
    )


def h12_invalidations_report(years: int = 4, limit: int = 10) -> ResearchReport:
    """Last N H12 SFP invalidations with forward post-invalidation outcomes."""
    bars = ohlc_cache.get_h12_bars(years=years)
    if not bars:
        raise RuntimeError("No H12 bars available — run backfill.py first.")

    all_events = detect_sfps(bars, timeframe="H12")
    invalidated = [e for e in all_events if e.outcome_a == "invalidation"]
    invalidated.sort(key=lambda e: e.ts)
    selected = invalidated[-limit:]

    if not selected:
        return ResearchReport(
            topic="h12_invalidations",
            title="H12 Invalidation Study",
            headline=f"No scored H12 SFP invalidations in the past {years} years.",
            sections=[
                ("Metrics", [
                    f"• Total H12 SFPs detected: {len(all_events)}",
                    f"• Invalidations: 0",
                ]),
                ("Methodology", [_INVALIDATION_METHODOLOGY]),
            ],
            interpretation=[
                "Try a longer lookback or run backfill.py --all for more H1 history.",
            ],
            sources=["Coinbase OHLC (ohlc.db)", "patterns/sfp.py"],
        )

    df = research.to_dataframe(bars)
    followups = [score_post_invalidation(df, event) for event in selected]
    stats = compute_invalidation_stats(followups)

    # Chart uses SFP stats shape for marker colors; events are all invalidations (red).
    chart_stats = {
        "reversal_pct": 0,
        "total_sfps": len(selected),
        "reversals": 0,
        "invalidations": len(selected),
        "neutral": 0,
        "pending": 0,
        "outcome_b_pct": 0,
        "outcome_c_pct": 0,
    }
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_start = df.index[0].strftime("%Y-%m")
    date_end = df.index[-1].strftime("%Y-%m")
    panel = (
        f"H12 Invalidation Follow-up\n\n"
        f"Period: {date_start} to {date_end}\n"
        f"Last {len(selected)} invalidated SFPs\n\n"
        f"Post-invalidation (N={len(selected)}):\n"
        f"  {stats['continuation_pct']}% continuation\n"
        f"  {stats['continuation']} cont / {stats['mean_reversion']} fade\n"
        f"  {stats['neutral']} neutral, {stats['pending']} pending\n\n"
        f"Red markers = invalidated SFPs"
    )
    chart_path = charts.render_research_chart(
        bars,
        selected,
        chart_stats,
        timeframe="H12",
        cycle_id=f"{cycle_id}_inv",
        years=years,
        title_override=f"ETH-USD H12 — Invalidation Study (last {len(selected)})",
        panel_text=panel,
    )

    headline = (
        f"Last {len(selected)} H12 SFP invalidations ({years}y): "
        f"{stats['continuation_pct']}% continued in invalidation direction"
    )
    metrics = [
        f"• Total H12 SFPs in window: {len(all_events)}",
        f"• Invalidations in window: {len(invalidated)}",
        f"• Studied (most recent): {len(selected)}",
        f"• Post-invalidation continuation: {stats['continuation']} ({stats['continuation_pct']}%)",
        f"• Post-invalidation mean reversion: {stats['mean_reversion']}",
        f"• Neutral / pending: {stats['neutral']} / {stats['pending']}",
    ]
    event_lines = [_format_followup_line(fu) for fu in followups] or ["• None in window"]

    interpretation = [
        "Continuation = price extended in the invalidation direction after the failed SFP.",
        "Mean reversion = price faded back toward the original SFP thesis.",
        "Use as context for failed SFP follow-through — not a standalone signal.",
    ]

    caption = (
        f"H12 Invalidations — last {len(selected)}\n"
        f"{stats['continuation_pct']}% post-inv continuation\n"
        f"{stats['mean_reversion']} mean reversion"
    )[:1024]

    return ResearchReport(
        topic="h12_invalidations",
        title="H12 Invalidation Study",
        headline=headline,
        sections=[
            ("Metrics", metrics),
            ("Events (oldest → newest)", event_lines),
            ("Methodology", [_INVALIDATION_METHODOLOGY]),
        ],
        interpretation=interpretation,
        sources=["Coinbase OHLC (ohlc.db)", "patterns/sfp.py", "patterns/invalidation_followup.py"],
        chart_path=chart_path,
        caption=caption,
    )


if __name__ == "__main__":
    print("Running H12 SFP report...")
    result = h12_sfp_report(years=4)
    print(result.detail_text)
    print(f"\nChart: {result.chart_path}")
