"""Research topic catalog and natural-language routing keywords."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TopicSpec:
    topic_id: str
    label: str
    description: str
    category: str  # snapshot | study | coming_soon
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


TOPICS: dict[str, TopicSpec] = {
    "digest": TopicSpec(
        topic_id="digest",
        label="Market digest",
        description="full context digest (macro + funding + volume + dominance + miner)",
        category="snapshot",
        aliases=("context", "overview", "market"),
        keywords=("full digest", "market digest", "context digest", "research digest"),
    ),
    "macro": TopicSpec(
        topic_id="macro",
        label="Macro",
        description="headlines + posture + pulses",
        category="snapshot",
        aliases=("headlines", "news"),
        keywords=("macro", "headlines", "macro posture", "macro news", "macro pulse"),
    ),
    "funding": TopicSpec(
        topic_id="funding",
        label="Funding",
        description="ETH perp funding",
        category="snapshot",
        aliases=("funding_rate", "funding-rate"),
        keywords=("funding rate", "funding", "perp funding", "eth funding"),
    ),
    "volume": TopicSpec(
        topic_id="volume",
        label="Volume",
        description="spot vs perp 24h volume",
        category="snapshot",
        aliases=("vol", "liquidity"),
        keywords=("spot volume", "perp volume", "volume comparison", "spot vs perp"),
    ),
    "dominance": TopicSpec(
        topic_id="dominance",
        label="Dominance",
        description="BTC.D + USDT.D",
        category="snapshot",
        aliases=("btc_d", "usdt_d", "btc.d", "usdt.d"),
        keywords=("btc dominance", "usdt dominance", "dominance", "btc.d", "usdt.d"),
    ),
    "miner": TopicSpec(
        topic_id="miner",
        label="Miner breakeven",
        description="BTC miner breakeven (est.)",
        category="snapshot",
        aliases=("miner_breakeven", "hashprice", "breakeven"),
        keywords=("miner breakeven", "hashprice", "mining breakeven", "miner cost"),
    ),
    "h12_sfp": TopicSpec(
        topic_id="h12_sfp",
        label="H12 SFP study",
        description="H12 SFP reversal study (chart + stats)",
        category="study",
        aliases=("h12-sfp", "h12", "sfp"),
        keywords=("h12 sfp", "12h sfp", "12-hour sfp", "h12 sfp reversal"),
    ),
    "weekly_sfp": TopicSpec(
        topic_id="weekly_sfp",
        label="Weekly SFP study",
        description="weekly SFP reversal study (chart + stats)",
        category="study",
        aliases=("weekly-sfp", "weekly", "w1_sfp"),
        keywords=("weekly sfp", "week sfp", "w1 sfp", "weekly sfp reversal"),
    ),
    "h12_invalidations": TopicSpec(
        topic_id="h12_invalidations",
        label="H12 invalidations",
        description="last 10 H12 SFP invalidations + forward outcomes",
        category="coming_soon",
        aliases=("h12-invalidation", "invalidations"),
        keywords=("h12 invalidation", "invalidated sfp", "sfp invalidated"),
    ),
}

# SFP reversal NL patterns (study routing)
_SFP_REVERSAL_PATTERN = re.compile(
    r"(%.*sfp|sfp.*%|sfp.*reversal|reversal.*sfp|sfp.*past|past.*sfp|how\s+many\s+sfp)",
    re.IGNORECASE,
)


def format_catalog() -> str:
    lines = [
        "ETH Research",
        "",
        "Market snapshot",
    ]
    for spec in TOPICS.values():
        if spec.category == "snapshot":
            lines.append(f"  /research {spec.topic_id} — {spec.description}")
    lines.extend(["", "Pattern studies (chart + stats)"])
    for spec in TOPICS.values():
        if spec.category == "study":
            lines.append(f"  /research {spec.topic_id} — {spec.description}")
    lines.extend(["", "Coming soon"])
    for spec in TOPICS.values():
        if spec.category == "coming_soon":
            lines.append(f"  /research {spec.topic_id} — {spec.description}")
    lines.extend(
        [
            "",
            "Examples:",
            '  "What\'s ETH funding right now?"',
            '  "BTC dominance and USDT dominance"',
            '  "What % of H12 SFPs reversed in 4 years?"',
            "",
            "Not financial advice.",
        ]
    )
    return "\n".join(lines)


def topic_from_token(token: str) -> str | None:
    normalized = token.strip().lower().replace("-", "_")
    if normalized in TOPICS:
        return normalized
    for spec in TOPICS.values():
        if normalized in spec.aliases:
            return spec.topic_id
    return None


def match_topic_from_text(text: str) -> str | None:
    """Resolve a research topic from command args or natural language."""
    normalized = text.strip().lower()
    if normalized.startswith("/research"):
        parts = normalized.split(maxsplit=1)
        if len(parts) == 1:
            return None
        remainder = parts[1].strip()
        if not remainder:
            return None
        first = remainder.split()[0]
        topic = topic_from_token(first)
        if topic:
            return topic

    for spec in TOPICS.values():
        for kw in spec.keywords:
            if kw in normalized:
                return spec.topic_id

    if _SFP_REVERSAL_PATTERN.search(normalized):
        if "weekly" in normalized or "w1" in normalized:
            return "weekly_sfp"
        if "h12" in normalized or "12h" in normalized or "12-hour" in normalized:
            return "h12_sfp"
        return "h12_sfp"

    return None


def is_research_query(text: str) -> bool:
    normalized = text.strip().lower()
    if normalized.startswith("/research"):
        return True
    if match_topic_from_text(text):
        return True
    return bool(_SFP_REVERSAL_PATTERN.search(normalized))
