"""Research analytics orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone

import bot_config
import charts
import ohlc_cache
import research
from patterns.invalidation_followup import (
    InvalidationFollowUp,
    compute_invalidation_stats,
    score_post_invalidation,
)
from patterns.sfp import SFPEvent, compute_stats, detect_sfps
from patterns import sfp_index
from research_reports.format import ResearchReport

_DEFAULT_PRODUCT = bot_config.DEFAULT_PRODUCT_ID


def _methodology(timeframe: str, product_id: str) -> str:
    templates = {
        "W1": (
            f"Methodology: Coinbase {product_id}, weekly W-FRI bars. "
            "SFP = L=3 pivot, wick sweeps >=0.2% past a swing from the last ~20 weeks, close back inside. "
            "Outcome A = >=2% follow-through from event close within N bars (or invalidation if close past level). "
            "B/C = >=5% move / structure break (same window)."
        ),
        "D1": (
            f"Methodology: Coinbase {product_id}, daily bars. "
            "SFP = L=3 pivot, wick sweeps >=0.25% past a swing from the last ~60 days, close back inside. "
            "Outcome A = >=1.5% follow-through from event close within N bars (or invalidation). "
            "B/C = >=5% move / structure break (same window)."
        ),
        "H12": (
            f"Methodology: Coinbase {product_id}, 12h bars resampled from H1. "
            "SFP = L=4 extreme pivot, latest swing swept >=0.3% within ~3 weeks, close back inside. "
            "Outcome A = >=1.5% follow-through from event close within N bars (or invalidation). "
            "B/C = >=5% move / structure break (same window)."
        ),
    }
    return templates.get(timeframe, templates["W1"])


_LABELS: dict[str, str] = {
    "W1": "Weekly",
    "D1": "Daily",
    "H12": "H12",
}

_TOPIC_IDS: dict[str, str] = {
    "W1": "weekly_sfp",
    "D1": "d1_sfps",
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
    product_id: str,
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
    return [
        ("Metrics", metrics),
        ("Recent events", recent_lines[1:] if len(recent_lines) > 1 else ["• None"]),
        ("Methodology", [_methodology(timeframe, product_id)]),
    ]


def _build_caption(stats: dict, years: int, timeframe: str, product_id: str) -> str:
    label = _LABELS.get(timeframe, timeframe)
    scored = stats["reversals"] + stats["invalidations"]
    return (
        f"{label} SFP — {years}y {product_id}\n"
        f"{stats['reversal_pct']}% reversal ({stats['reversals']}/{scored} scored)\n"
        f"{stats['total_sfps']} SFPs detected"
    )[:1024]


def _load_bars(timeframe: str, years: int, product_id: str) -> list[dict[str, float | str]]:
    tf = timeframe.upper()
    if tf == "W1":
        return ohlc_cache.get_weekly_bars(years=years, product_id=product_id)
    if tf == "D1":
        return ohlc_cache.get_daily_bars(years=years, product_id=product_id)
    if tf == "H12":
        return ohlc_cache.get_h12_bars(years=years, product_id=product_id)
    raise ValueError(f"Unsupported research timeframe: {timeframe}")


def sfp_report(
    timeframe: str = "W1",
    years: int = 4,
    *,
    product_id: str = _DEFAULT_PRODUCT,
) -> ResearchReport:
    """Run SFP study for W1, D1, or H12: cache -> detect -> index -> chart -> summary."""
    tf = timeframe.upper()
    bars = _load_bars(tf, years, product_id)

    if not bars:
        raise RuntimeError(
            f"No {tf} bars available for {product_id} — run backfill.py --product {product_id} first."
        )

    events = detect_sfps(bars, timeframe=tf)
    stats = compute_stats(events)
    sfp_index.rebuild_sfp_index(product_id, tf, years)

    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    chart_path = charts.render_research_chart(
        bars,
        events,
        stats,
        timeframe=tf,
        cycle_id=cycle_id,
        years=years,
        title_override=f"{product_id} {_LABELS.get(tf, tf)} SFP Study ({years}y)",
    )

    label = _LABELS.get(tf, tf)
    scored = stats["reversals"] + stats["invalidations"]
    headline = (
        f"{label} SFP reversal study ({years} years, {product_id}): "
        f"{stats['reversal_pct']}% reversal ({stats['reversals']}/{scored} scored)"
    )

    interpretation = [
        f"{stats['total_sfps']} SFPs detected over {years} years on Coinbase {product_id}.",
        "Outcome A measures reversal vs invalidation within the scoring window.",
        "Counts are grounded in the deterministic SFP index — not LLM memory.",
        "Use as historical context — not a standalone trade signal.",
    ]

    return ResearchReport(
        topic=_TOPIC_IDS.get(tf, tf.lower()),
        title=f"{label} SFP Study",
        headline=headline,
        sections=_build_sections(stats, years, events, tf, len(bars), product_id),
        interpretation=interpretation,
        sources=["Coinbase OHLC (ohlc.db)", "patterns/sfp.py", "patterns/sfp_index.py"],
        chart_path=chart_path,
        caption=_build_caption(stats, years, tf, product_id),
    )


def weekly_sfp_report(
    years: int = 4,
    *,
    product_id: str = _DEFAULT_PRODUCT,
) -> ResearchReport:
    """Run weekly SFP study: cache -> detect -> chart -> summary."""
    return sfp_report("W1", years=years, product_id=product_id)


def daily_sfp_report(
    years: int = 4,
    *,
    product_id: str = _DEFAULT_PRODUCT,
) -> ResearchReport:
    """Run daily SFP study: cache -> detect -> chart -> summary."""
    return sfp_report("D1", years=years, product_id=product_id)


def h12_sfp_report(
    years: int = 4,
    *,
    product_id: str = _DEFAULT_PRODUCT,
) -> ResearchReport:
    """Run H12 SFP study: cache -> detect -> chart -> summary."""
    return sfp_report("H12", years=years, product_id=product_id)


def _invalidation_methodology(timeframe: str, product_id: str) -> str:
    move = "1.5%" if timeframe in ("H12", "D1") else "2%"
    return (
        f"Methodology: Coinbase {product_id} {timeframe} bars. "
        "Select SFPs where Outcome A = invalidation (close past swept level within N bars). "
        f"Post-invalidation: continuation = >={move} move in invalidation direction from inv close; "
        f"mean_reversion = >={move} fade back toward original SFP thesis (same N-bar window)."
    )


def _format_followup_line(fu: InvalidationFollowUp) -> str:
    e = fu.event
    move = f", {fu.move_pct:.1f}% move" if fu.move_pct is not None else ""
    return (
        f"  {e.ts[:10]} {e.direction} @ {e.swept_level:,.0f} "
        f"-> post-inv: {fu.outcome}{move}"
    )


def _invalidations_report(
    timeframe: str,
    years: int = 4,
    limit: int = 10,
    *,
    product_id: str = _DEFAULT_PRODUCT,
    topic_id: str,
    title: str,
) -> ResearchReport:
    """Last N SFP invalidations with forward post-invalidation outcomes."""
    tf = timeframe.upper()
    bars = _load_bars(tf, years, product_id)
    if not bars:
        raise RuntimeError(
            f"No {tf} bars available for {product_id} — run backfill.py --product {product_id} first."
        )

    all_events = detect_sfps(bars, timeframe=tf)
    sfp_index.rebuild_sfp_index(product_id, tf, years)
    invalidated = [e for e in all_events if e.outcome_a == "invalidation"]
    invalidated.sort(key=lambda e: e.ts)
    selected = invalidated[-limit:]
    methodology = _invalidation_methodology(tf, product_id)

    if not selected:
        return ResearchReport(
            topic=topic_id,
            title=title,
            headline=f"No scored {tf} SFP invalidations in the past {years} years ({product_id}).",
            sections=[
                ("Metrics", [
                    f"• Total {tf} SFPs detected: {len(all_events)}",
                    "• Invalidations: 0",
                ]),
                ("Methodology", [methodology]),
            ],
            interpretation=[
                f"Try a longer lookback or run backfill.py --product {product_id} --all.",
            ],
            sources=["Coinbase OHLC (ohlc.db)", "patterns/sfp.py", "patterns/sfp_index.py"],
        )

    df = research.to_dataframe(bars)
    followups = [score_post_invalidation(df, event) for event in selected]
    stats = compute_invalidation_stats(followups)

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
    label = _LABELS.get(tf, tf)
    panel = (
        f"{label} Invalidation Follow-up\n\n"
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
        timeframe=tf,
        cycle_id=f"{cycle_id}_inv",
        years=years,
        title_override=f"{product_id} {tf} — Invalidation Study (last {len(selected)})",
        panel_text=panel,
    )

    headline = (
        f"Last {len(selected)} {tf} SFP invalidations ({years}y, {product_id}): "
        f"{stats['continuation_pct']}% continued in invalidation direction"
    )
    metrics = [
        f"• Total {tf} SFPs in window: {len(all_events)}",
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
        f"{tf} Invalidations — last {len(selected)} ({product_id})\n"
        f"{stats['continuation_pct']}% post-inv continuation\n"
        f"{stats['mean_reversion']} mean reversion"
    )[:1024]

    return ResearchReport(
        topic=topic_id,
        title=title,
        headline=headline,
        sections=[
            ("Metrics", metrics),
            ("Events (oldest → newest)", event_lines),
            ("Methodology", [methodology]),
        ],
        interpretation=interpretation,
        sources=[
            "Coinbase OHLC (ohlc.db)",
            "patterns/sfp.py",
            "patterns/sfp_index.py",
            "patterns/invalidation_followup.py",
        ],
        chart_path=chart_path,
        caption=caption,
    )


def h12_invalidations_report(
    years: int = 4,
    limit: int = 10,
    *,
    product_id: str = _DEFAULT_PRODUCT,
) -> ResearchReport:
    """Last N H12 SFP invalidations with forward post-invalidation outcomes."""
    return _invalidations_report(
        "H12",
        years=years,
        limit=limit,
        product_id=product_id,
        topic_id="h12_invalidations",
        title="H12 Invalidation Study",
    )


def weekly_invalidations_report(
    years: int = 4,
    limit: int = 10,
    *,
    product_id: str = _DEFAULT_PRODUCT,
) -> ResearchReport:
    """Last N weekly SFP invalidations with forward post-invalidation outcomes."""
    return _invalidations_report(
        "W1",
        years=years,
        limit=limit,
        product_id=product_id,
        topic_id="w1_invalidations",
        title="Weekly Invalidation Study",
    )


if __name__ == "__main__":
    print("Running H12 SFP report...")
    result = h12_sfp_report(years=4)
    print(result.detail_text)
    print(f"\nChart: {result.chart_path}")
