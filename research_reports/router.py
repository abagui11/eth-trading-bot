"""Route /research topics to report builders."""

from __future__ import annotations

import re
from typing import Callable

import analytics
from research_reports.catalog import TOPICS, format_catalog, match_topic_from_text
from research_reports.format import ResearchReport
from research_reports.topics import digest, dominance, funding, macro, miner, volume

ReportBuilder = Callable[[], ResearchReport]
StudyBuilder = Callable[[int], ResearchReport]

_SNAPSHOT_BUILDERS: dict[str, ReportBuilder] = {
    "digest": digest.build_digest_report,
    "macro": macro.build_macro_report,
    "funding": funding.build_funding_report,
    "volume": volume.build_volume_report,
    "dominance": dominance.build_dominance_report,
    "miner": miner.build_miner_report,
}

_STUDY_BUILDERS: dict[str, StudyBuilder] = {
    "h12_sfp": analytics.h12_sfp_report,
    "weekly_sfp": analytics.weekly_sfp_report,
}


def resolve_topic(text: str) -> str | None:
    return match_topic_from_text(text)


def parse_years(text: str, default: int = 4) -> int:
    match = re.search(r"(\d+)\s*years?", text, re.IGNORECASE)
    if match:
        return max(1, min(int(match.group(1)), 10))
    return default


def build_catalog() -> str:
    return format_catalog()


def build_report(topic_id: str, *, years: int = 4) -> ResearchReport:
    spec = TOPICS.get(topic_id)
    if spec is None:
        raise ValueError(f"Unknown research topic: {topic_id}")

    if spec.category == "coming_soon":
        return ResearchReport(
            topic=topic_id,
            title=spec.label,
            headline=f"{spec.label} is not available yet.",
            interpretation=[
                "This study is on the roadmap. Use /research for the full topic catalog.",
            ],
            sources=["Trading Guide backlog"],
        )

    if topic_id in _SNAPSHOT_BUILDERS:
        return _SNAPSHOT_BUILDERS[topic_id]()

    if topic_id in _STUDY_BUILDERS:
        return _STUDY_BUILDERS[topic_id](years)

    raise ValueError(f"No builder registered for topic: {topic_id}")


def topic_status_message(topic_id: str) -> str | None:
    """Return a short 'working on it' message for long-running studies."""
    spec = TOPICS.get(topic_id)
    if spec and spec.category == "study":
        if topic_id == "weekly_sfp":
            return "Analyzing weekly SFPs..."
        return "Analyzing H12 SFPs..."
    if spec and spec.category == "snapshot":
        return f"Building {spec.label.lower()} report..."
    return None
