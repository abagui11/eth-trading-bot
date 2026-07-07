"""LLM classification for promoted macro headlines."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic
import bot_config
import config

logger = logging.getLogger(__name__)

CLASSIFIER_SYSTEM = """You classify financial headlines for an ETH swing-trading bot.
Chart structure is primary; your output is supplementary macro context only.

Return JSON only with these fields:
- severity (integer 1-5): 1=ETH-irrelevant noise, 2=minor macro, 3=moderate (hourly context),
  4=high vol (risk pulse), 5=extreme (exchange halt, major war, ETF shock)
- eth_bias: one of bullish, bearish, neutral, mixed
- category: short snake_case label (e.g. geopolitical_energy, monetary_policy, crypto_regulation)
- eth_impact_summary: one sentence on likely ETH impact
- ttl_hours: integer hours this headline stays relevant (12-72 typical)
- posture_hints: array of zero or more: avoid_new_long, avoid_new_short, tighten_long_stops,
  tighten_short_stops, consider_close_long, consider_close_short, size_up_if_confirmed

Do not recommend trades. Assess headline impact only."""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def classify_headline(
    *,
    title: str,
    summary: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Run Haiku/Anthropic classifier on a promoted headline."""
    parts = [f"Headline: {title}"]
    if source:
        parts.append(f"Source: {source}")
    if summary:
        parts.append(f"Summary: {summary}")
    parts.append("Return JSON only.")

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=512,
            system=CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )
    except Exception as exc:
        logger.exception("Macro classifier API failed")
        return {
            "severity": 2,
            "eth_bias": "neutral",
            "category": "unknown",
            "eth_impact_summary": f"classifier_error: {exc}",
            "ttl_hours": bot_config.MACRO_DEFAULT_TTL_HOURS,
            "posture_hints": [],
        }

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw += block.text

    try:
        data = _extract_json(raw)
    except json.JSONDecodeError:
        logger.warning("Macro classifier bad JSON: %s", raw[:300])
        return {
            "severity": 2,
            "eth_bias": "neutral",
            "category": "parse_error",
            "eth_impact_summary": "Could not parse classifier output",
            "ttl_hours": bot_config.MACRO_DEFAULT_TTL_HOURS,
            "posture_hints": [],
        }

    severity = max(1, min(5, int(data.get("severity", 2))))
    bias = str(data.get("eth_bias", "neutral")).lower()
    if bias not in ("bullish", "bearish", "neutral", "mixed"):
        bias = "neutral"
    ttl = int(data.get("ttl_hours", bot_config.MACRO_DEFAULT_TTL_HOURS))
    ttl = max(6, min(168, ttl))
    hints = data.get("posture_hints") or []
    if not isinstance(hints, list):
        hints = []
    return {
        "severity": severity,
        "eth_bias": bias,
        "category": str(data.get("category", "macro")),
        "eth_impact_summary": str(data.get("eth_impact_summary", "")).strip(),
        "ttl_hours": ttl,
        "posture_hints": [str(h) for h in hints],
    }


def expires_at_from_ttl(ttl_hours: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
