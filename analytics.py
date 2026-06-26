"""Research analytics orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import charts
import ohlc_cache
from patterns.sfp import SFPEvent, compute_stats, detect_sfps

_METHODOLOGY: dict[str, str] = {
    "W1": (
        "Methodology: Coinbase ETH-USD, weekly W-FRI bars. "
        "SFP = L=3 pivot, wick sweeps >=0.2% past a swing from the last ~20 weeks, close back inside. "
        "Outcome A = >=2% follow-through from event close within N bars (or invalidation if close past level). "
        "B/C = >=5% move / structure break (same window). Not financial advice."
    ),
    "H12": (
        "Methodology: Coinbase ETH-USD, 12h bars resampled from H1. "
        "SFP = L=4 extreme pivot, latest swing swept >=0.3% within ~3 weeks, close back inside. "
        "Outcome A = >=1.5% follow-through from event close within N bars (or invalidation). "
        "B/C = >=5% move / structure break (same window). Not financial advice."
    ),
}

_LABELS: dict[str, str] = {
    "W1": "Weekly",
    "H12": "H12",
}


@dataclass
class ResearchResult:
    chart_path: str
    summary_text: str
    caption: str
    events: list[SFPEvent]
    stats: dict
    years: int
    timeframe: str = "W1"


def _bar_count_note(timeframe: str, bar_count: int, years: int) -> str:
    per_year = round(bar_count / years, 1) if years else bar_count
    return f"Bars analyzed: {bar_count} (~{per_year}/year)"


def _format_summary(
    stats: dict,
    years: int,
    events: list[SFPEvent],
    timeframe: str,
    bar_count: int,
) -> str:
    label = _LABELS.get(timeframe, timeframe)
    lines = [
        f"{label} SFP reversal study ({years} years)",
        _bar_count_note(timeframe, bar_count, years),
        "",
        f"Headline (Outcome A): {stats['reversal_pct']}% reversal",
        f"  {stats['reversals']} reversals / {stats['invalidations']} invalidations",
        f"  ({stats['reversals'] + stats['invalidations']} scored; "
        f"{stats['neutral']} neutral, {stats['pending']} pending)",
        "",
        f"Total SFPs detected: {stats['total_sfps']}",
        f"Outcome B (>=5% move in direction): {stats['outcome_b_pct']}% "
        f"({stats['outcome_b_count']}/{stats['outcome_bc_eligible']})",
        f"Outcome C (structure break): {stats['outcome_c_pct']}% "
        f"({stats['outcome_c_count']}/{stats['outcome_bc_eligible']})",
        "",
        "Recent events:",
    ]
    recent = sorted(events, key=lambda e: e.ts)[-5:]
    for e in recent:
        lines.append(
            f"  {e.ts[:10]} {e.direction} @ {e.swept_level:,.0f} -> {e.outcome_a}"
        )
    footnote = _METHODOLOGY.get(timeframe, _METHODOLOGY["W1"])
    lines.extend(["", footnote])
    return "\n".join(lines)


def _build_caption(stats: dict, years: int, timeframe: str) -> str:
    label = _LABELS.get(timeframe, timeframe)
    scored = stats["reversals"] + stats["invalidations"]
    return (
        f"{label} SFP — {years}y ETH-USD\n"
        f"{stats['reversal_pct']}% reversal ({stats['reversals']}/{scored} scored)\n"
        f"{stats['total_sfps']} SFPs detected"
    )[:1024]


def sfp_report(timeframe: str = "W1", years: int = 4) -> ResearchResult:
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

    summary = _format_summary(stats, years, events, tf, len(bars))
    caption = _build_caption(stats, years, tf)

    return ResearchResult(
        chart_path=chart_path,
        summary_text=summary,
        caption=caption,
        events=events,
        stats=stats,
        years=years,
        timeframe=tf,
    )


def weekly_sfp_report(years: int = 4) -> ResearchResult:
    """Run weekly SFP study: cache -> detect -> chart -> summary."""
    return sfp_report("W1", years=years)


def h12_sfp_report(years: int = 4) -> ResearchResult:
    """Run H12 SFP study: cache -> detect -> chart -> summary."""
    return sfp_report("H12", years=years)


if __name__ == "__main__":
    print("Running H12 SFP report...")
    result = h12_sfp_report(years=4)
    print(result.summary_text)
    print(f"\nChart: {result.chart_path}")
