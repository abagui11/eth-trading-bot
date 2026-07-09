"""Macro snapshot research report."""

from __future__ import annotations

import bot_config
from macro import store
from macro.context import active_posture
from research_reports.format import ResearchReport


def build_macro_report() -> ResearchReport:
    if not bot_config.MACRO_CONTEXT_ENABLED:
        return ResearchReport(
            topic="macro",
            title="Macro",
            headline="Macro context layer is disabled.",
            interpretation=[
                "Enable MACRO_CONTEXT_ENABLED to ingest headlines and posture advisories.",
            ],
            sources=["macro/store.py"],
        )

    posture = active_posture()
    events = posture.get("events") or []
    bias = posture.get("eth_bias", "neutral")
    max_sev = int(posture.get("max_severity") or 0)

    headline = f"{bias.capitalize()} macro posture | max severity {max_sev}"
    if events:
        headline += f" | {len(events)} active headline(s)"
    else:
        headline += " | no active headlines"

    posture_bullets = [
        f"• ETH bias: {bias}",
        f"• Max severity: {max_sev}",
        f"• Watchdog gate long: {posture.get('gate_long', False)}",
        f"• Watchdog gate short: {posture.get('gate_short', False)}",
    ]

    headline_bullets: list[str] = []
    pulse_bullets: list[str] = []
    for event in events[:5]:
        sev = int(event.get("severity") or 0)
        ebias = event.get("eth_bias") or "neutral"
        title = str(event.get("title") or "").strip()
        headline_bullets.append(f"• [SEV {sev} | {ebias}] {title[:120]}")
        impact = str(event.get("eth_impact_summary") or "").strip()
        if impact:
            headline_bullets.append(f"  Impact: {impact[:200]}")
        pulse = store.get_latest_pulse_for_event(int(event["id"]))
        if pulse and pulse.get("text_summary"):
            pulse_bullets.append(
                f"• ({pulse.get('ts', 'n/a')}) {str(pulse['text_summary'])[:200]}"
            )

    if not headline_bullets:
        headline_bullets = ["• No classified headlines above inject threshold."]

    sections: list[tuple[str, list[str]]] = [("Posture", posture_bullets), ("Active headlines", headline_bullets)]
    if pulse_bullets:
        sections.append(("Latest pulses", pulse_bullets))

    interpretation = [
        "Chart structure remains primary; macro is a supplementary risk filter.",
        "High-severity bearish macro may warrant tighter stops — not automatic flat.",
    ]
    if not events:
        interpretation = [
            "No active macro headlines — rely on programmatic chart structure.",
        ]

    return ResearchReport(
        topic="macro",
        title="Macro",
        headline=headline,
        sections=sections,
        interpretation=interpretation,
        sources=["macro RSS/webhook ingest", "macro/store.py"],
    )
