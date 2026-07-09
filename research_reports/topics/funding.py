"""ETH perp funding research report."""

from __future__ import annotations

from metrics import fetch
from research_reports.format import ResearchReport


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.4f}%"


def build_funding_report() -> ResearchReport:
    try:
        snap = fetch.fetch_funding()
    except Exception as exc:
        return ResearchReport(
            topic="funding",
            title="ETH Funding",
            headline="Funding data temporarily unavailable.",
            sections=[("Error", [f"• {exc}"])],
            interpretation=["Retry later or check Binance API connectivity."],
            sources=["Binance Futures API"],
        )

    headline = f"ETH perp funding {snap.current_rate_pct:+.4f}% (8h rate, {snap.symbol})"
    metrics = [
        f"• Current rate: {_fmt_pct(snap.current_rate_pct)} per 8h",
        f"• 7d average: {_fmt_pct(snap.avg_7d_pct)}",
        f"• 7d range: {_fmt_pct(snap.min_7d_pct)} to {_fmt_pct(snap.max_7d_pct)}",
    ]
    if snap.next_funding_time:
        metrics.append(f"• Next funding window: {snap.next_funding_time}")

    interpretation: list[str] = []
    if snap.current_rate_pct > 0.03:
        interpretation.append("Elevated positive funding — longs paying shorts; crowded long risk.")
    elif snap.current_rate_pct < -0.03:
        interpretation.append("Negative funding — shorts paying longs; squeeze risk on rallies.")
    else:
        interpretation.append("Funding near neutral — no extreme positioning signal from rates alone.")
    interpretation.append("Use funding as confirmation only; chart structure drives entries.")

    return ResearchReport(
        topic="funding",
        title="ETH Funding",
        headline=headline,
        sections=[("Metrics", metrics)],
        interpretation=interpretation,
        sources=["Binance Futures API (ETHUSDT)"],
    )
