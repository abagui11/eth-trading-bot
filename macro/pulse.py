"""Position-aware macro pulse advisory (no execution)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic
import audit
import bot_config
import config
import notify
import paper
import research
from macro import store

logger = logging.getLogger(__name__)

PULSE_SYSTEM = """You are a risk advisor for an ETH paper-trading bot — not a trader.
Given a macro headline and any open paper positions, recommend posture only.

Chart structure is primary; macro is supplementary. Do not recommend new entries.

Return JSON only:
- recommendation: one of hold, tighten_sl, consider_close, avoid_add
- rationale: 2-4 sentences for the operator
- per_position: array of {position_id, side, action, note} when positions are open
  (action: hold | tighten_sl | consider_close | avoid_add)

Be measured — avoid panic. Macro alone should rarely demand immediate flat."""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def run_macro_pulse(event: dict[str, Any]) -> dict[str, Any] | None:
    """Run advisory pulse for a classified high-severity event."""
    if int(event.get("severity") or 0) < bot_config.MACRO_PULSE_MIN_SEVERITY:
        return None

    spot = research.get_spot_price()
    open_positions = paper.get_open_positions(spot)
    position_detail = paper.format_positions_detail(spot)

    market_snippet = ""
    snapshot = audit.get_latest_snapshot()
    if snapshot:
        snap = snapshot.get("snapshot") or {}
        market_snippet = str(snap.get("summary_text") or "")[:2000]

    parts = [
        f"Headline: {event.get('title')}",
        f"Severity: {event.get('severity')} | Bias: {event.get('eth_bias')}",
        f"Impact: {event.get('eth_impact_summary')}",
    ]
    if position_detail:
        parts.append(f"Open positions:\n{position_detail}")
    else:
        parts.append("Open positions: none")
    if market_snippet:
        parts.append(f"Latest market context excerpt:\n{market_snippet}")

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=768,
            system=PULSE_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )
    except Exception:
        logger.exception("Macro pulse API failed")
        return None

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw += block.text

    try:
        advisory = _extract_json(raw)
    except json.JSONDecodeError:
        advisory = {"recommendation": "hold", "rationale": raw[:500], "per_position": []}

    text_summary = str(advisory.get("rationale") or "").strip()
    rec = str(advisory.get("recommendation") or "hold")
    if text_summary:
        text_summary = f"{rec}: {text_summary}"

    pulse_row = store.insert_pulse(
        event_id=int(event["id"]),
        open_positions=open_positions,
        advisory=advisory,
        text_summary=text_summary,
    )

    try:
        notify.send_macro_pulse_alert(event, advisory, text_summary)
    except Exception:
        logger.exception("Failed to send macro pulse alert")

    return pulse_row
