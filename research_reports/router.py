"""Route /research topics to report builders."""

from __future__ import annotations

import re
from typing import Callable

import analytics
import bot_config
from research_reports.catalog import (
    TOPICS,
    clarify_sfp_message,
    format_catalog,
    is_ambiguous_sfp_query,
    is_unsupported_pattern_query,
    match_topic_from_text,
    not_indexed_message,
)
from research_reports.format import ResearchReport
from research_reports.topics import digest, dominance, funding, macro, miner, volume

ReportBuilder = Callable[[], ResearchReport]

_SNAPSHOT_BUILDERS: dict[str, ReportBuilder] = {
    "digest": digest.build_digest_report,
    "macro": macro.build_macro_report,
    "funding": funding.build_funding_report,
    "volume": volume.build_volume_report,
    "dominance": dominance.build_dominance_report,
    "miner": miner.build_miner_report,
}


def resolve_topic(text: str) -> str | None:
    return match_topic_from_text(text)


def parse_years(text: str, default: int = 4) -> int:
    match = re.search(r"(\d+)\s*years?", text, re.IGNORECASE)
    if match:
        return max(1, min(int(match.group(1)), 10))
    # Bare number after topic token, e.g. "/research d1_sfps 5 BTC"
    tokens = text.strip().split()
    for tok in tokens[1:]:
        if re.fullmatch(r"\d+", tok):
            return max(1, min(int(tok), 10))
    return default


def parse_product_id(text: str, default: str = bot_config.DEFAULT_PRODUCT_ID) -> str:
    """Resolve ETH/BTC product from free text; default ETH-USD."""
    normalized = text.upper()
    if re.search(r"\bBTC(?:-USD)?\b", normalized):
        return "BTC-USD"
    if re.search(r"\bETH(?:-USD)?\b", normalized):
        return "ETH-USD"
    return default


def build_catalog() -> str:
    return format_catalog()


def parse_limit(text: str, default: int = 10) -> int:
    match = re.search(r"last\s+(\d+)", text, re.IGNORECASE)
    if match:
        return max(1, min(int(match.group(1)), 20))
    return default


def build_report(
    topic_id: str,
    *,
    years: int = 4,
    text: str = "",
    product_id: str | None = None,
) -> ResearchReport:
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

    product = product_id or parse_product_id(text)

    if topic_id == "h12_invalidations":
        return analytics.h12_invalidations_report(
            years,
            limit=parse_limit(text),
            product_id=product,
        )
    if topic_id == "w1_invalidations":
        return analytics.weekly_invalidations_report(
            years,
            limit=parse_limit(text),
            product_id=product,
        )
    if topic_id == "d1_sfps":
        return analytics.daily_sfp_report(years, product_id=product)
    if topic_id == "weekly_sfp":
        return analytics.weekly_sfp_report(years, product_id=product)
    if topic_id == "h12_sfp":
        return analytics.h12_sfp_report(years, product_id=product)

    raise ValueError(f"No builder registered for topic: {topic_id}")


def clarify_or_refuse(text: str) -> str | None:
    """Return a clarify/refuse message when we should not invent an answer."""
    if is_unsupported_pattern_query(text):
        return not_indexed_message()
    if is_ambiguous_sfp_query(text):
        return clarify_sfp_message()
    return None


def topic_status_message(topic_id: str) -> str | None:
    """Return a short 'working on it' message for long-running studies."""
    spec = TOPICS.get(topic_id)
    if spec and spec.category == "study":
        if topic_id == "weekly_sfp":
            return "Analyzing weekly SFPs..."
        if topic_id == "d1_sfps":
            return "Analyzing daily SFPs..."
        if topic_id == "h12_invalidations":
            return "Analyzing last H12 SFP invalidations..."
        if topic_id == "w1_invalidations":
            return "Analyzing last weekly SFP invalidations..."
        return "Analyzing H12 SFPs..."
    if spec and spec.category == "snapshot":
        return f"Building {spec.label.lower()} report..."
    return None
