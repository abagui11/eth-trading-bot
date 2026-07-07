"""Build macro advisory blocks and active posture for trading paths."""

from __future__ import annotations

from typing import Any

import bot_config
from macro import store


def active_posture() -> dict[str, Any]:
    """Aggregate active classified macro events into trading posture flags."""
    if not bot_config.MACRO_CONTEXT_ENABLED:
        return {
            "eth_bias": "neutral",
            "max_severity": 0,
            "gate_long": False,
            "gate_short": False,
            "events": [],
        }

    events = store.get_active_events(min_severity=bot_config.MACRO_MIN_SEVERITY_INJECT)
    if not events:
        return {
            "eth_bias": "neutral",
            "max_severity": 0,
            "gate_long": False,
            "gate_short": False,
            "events": [],
        }

    max_sev = max(int(e.get("severity") or 0) for e in events)
    biases = [str(e.get("eth_bias") or "neutral") for e in events if int(e.get("severity") or 0) >= 3]

    if biases.count("bearish") > biases.count("bullish"):
        eth_bias = "bearish"
    elif biases.count("bullish") > biases.count("bearish"):
        eth_bias = "bullish"
    elif "mixed" in biases:
        eth_bias = "mixed"
    else:
        eth_bias = "neutral"

    gate_threshold = bot_config.MACRO_WATCHDOG_GATE_MIN_SEVERITY
    gate_long = eth_bias == "bearish" and max_sev >= gate_threshold
    gate_short = eth_bias == "bullish" and max_sev >= gate_threshold

    return {
        "eth_bias": eth_bias,
        "max_severity": max_sev,
        "gate_long": gate_long,
        "gate_short": gate_short,
        "events": events,
    }


def build_macro_block() -> str:
    """Text block for LLM prompts (hourly, chat)."""
    if not bot_config.MACRO_CONTEXT_ENABLED:
        return ""

    posture = active_posture()
    events = posture["events"]
    if not events:
        return ""

    lines = [
        "=== Macro context (advisory — chart structure is primary) ===",
        (
            f"Active posture: {posture['eth_bias']} | max severity {posture['max_severity']}"
        ),
    ]

    for event in events[:5]:
        sev = int(event.get("severity") or 0)
        bias = event.get("eth_bias") or "neutral"
        category = event.get("category") or "macro"
        expires = event.get("expires_at") or "n/a"
        lines.append(
            f"[SEV {sev} | {bias} | {category} | expires {expires}]"
        )
        lines.append(f"- {event.get('title', '').strip()}")
        impact = str(event.get("eth_impact_summary") or "").strip()
        if impact:
            lines.append(f"  ETH impact: {impact}")
        hints = event.get("posture_hints") or []
        if hints:
            lines.append(f"  Posture hints: {', '.join(hints)}")

        pulse = store.get_latest_pulse_for_event(int(event["id"]))
        if pulse and pulse.get("text_summary"):
            lines.append(f"  Latest macro pulse ({pulse.get('ts')}): {pulse['text_summary']}")

    lines.extend(
        [
            "",
            "Macro rules:",
            "- Do not flip trade bias on macro alone; use as confirmation or risk filter.",
            "- When macro conflicts with structure, prefer no_trade or tighten existing positions.",
            "- Open positions: prefer tighten SL over panic flat unless structure also breaks.",
        ]
    )
    return "\n".join(lines)


def append_macro_to_lines(lines: list[str]) -> None:
    block = build_macro_block()
    if block:
        lines.append("")
        lines.append(block)


def macro_payload_for_dashboard() -> dict[str, Any]:
    """Dashboard API: active + recent macro headlines."""
    posture = active_posture()
    recent = store.list_events(limit=30)
    active = store.get_active_events(min_severity=bot_config.MACRO_MIN_SEVERITY_INJECT)
    return {
        "enabled": bot_config.MACRO_CONTEXT_ENABLED,
        "posture": {
            "eth_bias": posture["eth_bias"],
            "max_severity": posture["max_severity"],
            "gate_long": posture["gate_long"],
            "gate_short": posture["gate_short"],
        },
        "monitored_sources": store.get_monitored_feed_labels(),
        "active": [_event_summary(e) for e in active],
        "recent": [_event_summary(e) for e in recent],
    }


def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "title": event.get("title"),
        "url": event.get("url"),
        "source": event.get("source"),
        "ingested_at": event.get("ingested_at"),
        "published_at": event.get("published_at"),
        "keyword_score": event.get("keyword_score"),
        "severity": event.get("severity"),
        "eth_bias": event.get("eth_bias"),
        "category": event.get("category"),
        "status": event.get("status"),
        "eth_impact_summary": event.get("eth_impact_summary"),
        "expires_at": event.get("expires_at"),
        "posture_hints": event.get("posture_hints") or [],
    }
