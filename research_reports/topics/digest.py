"""Full market context digest — composes snapshot topics."""

from __future__ import annotations

from research_reports.format import ResearchReport
from research_reports.htf_chart import resolve_htf_chart
from research_reports.topics import dominance, funding, macro, miner, volume


def build_digest_report() -> ResearchReport:
    child_reports = [
        macro.build_macro_report(),
        funding.build_funding_report(),
        volume.build_volume_report(),
        dominance.build_dominance_report(),
        miner.build_miner_report(),
    ]

    headlines = [r.headline for r in child_reports if r.headline]
    headline = " | ".join(headlines[:3])
    if len(headlines) > 3:
        headline += " …"

    sections: list[tuple[str, list[str]]] = []
    all_interpretation: list[str] = []
    all_sources: list[str] = []

    for child in child_reports:
        sections.append((child.title, [child.headline]))
        for section_name, bullets in child.sections:
            abbreviated = bullets[:4]
            if abbreviated:
                sections.append((f"{child.title} — {section_name}", abbreviated))
        all_interpretation.extend(child.interpretation[:1])
        all_sources.extend(child.sources)

    unique_sources = list(dict.fromkeys(all_sources))

    chart_path, chart_caption = resolve_htf_chart()
    if chart_path:
        all_interpretation.insert(
            0,
            "H4 chart attached — zones, key levels, and detected structure match the hourly agent.",
        )

    return ResearchReport(
        topic="digest",
        title="Market Digest",
        headline=headline or "ETH market context digest",
        sections=sections,
        interpretation=all_interpretation[:5] or [
            "Chart structure remains primary for entries.",
        ],
        sources=unique_sources,
        chart_path=chart_path,
        caption=chart_caption or "ETH-USD H4 structure",
    )
