"""Claude Q&A about the latest trade suggestion."""

from __future__ import annotations

import logging
from pathlib import Path

import anthropic

import analyze
import config
import ledger
import paper
import research

logger = logging.getLogger(__name__)

_SYSTEM_SUFFIX = """
You are the ETH trading agent assistant. Answer only about:
- The agent's ICT swing strategy and rules
- The current or latest hourly trade suggestion
- Paper portfolio performance shown in the PnL line

Be concise and practical. If asked something off-topic or requiring historical backtests, say that is not available in this version. This is not financial advice.
"""


def _format_suggestion_context(row: dict) -> str:
    tps = ", ".join(f"{tp:,.2f}" for tp in row.get("take_profits", [])) or "n/a"
    return (
        f"Latest suggestion (cycle {row['cycle_id']}, {row['ts']}):\n"
        f"  action: {row['action']}\n"
        f"  entry: {row.get('entry')}\n"
        f"  stop_loss: {row.get('stop_loss')}\n"
        f"  take_profits: {tps}\n"
        f"  risk_reward: {row.get('risk_reward')}\n"
        f"  price_at_suggestion: {row.get('price_at_suggestion')}\n"
        f"  rationale: {row.get('rationale', '')}\n"
        f"  chart_path: {row.get('chart_path')}"
    )


def answer(user_message: str) -> str:
    """Return Claude's reply about the latest suggestion (caller appends PnL footer)."""
    rules = analyze.load_rules()
    latest = ledger.get_latest_suggestion()
    spot = research.get_spot_price()

    if latest is None:
        return (
            "No trade suggestions yet. The agent runs every hour — check back after the first cycle."
        )

    pnl_line = paper.format_pnl_footer(spot)
    text_context = (
        f"{_format_suggestion_context(latest)}\n\n"
        f"Current ETH spot: ${spot:,.2f}\n"
        f"{pnl_line}\n\n"
        f"User question: {user_message}"
    )

    chart_path = latest.get("chart_path")
    vision_blocks = analyze.build_vision_content(
        chart_paths=None,
        annotated_h1_path=chart_path if chart_path and Path(chart_path).exists() else None,
        include_live_charts=False,
        include_patterns=True,
    )

    user_content: list[dict] = [{"type": "text", "text": text_context}]
    user_content.extend(vision_blocks)

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": rules + "\n\n" + _SYSTEM_SUFFIX,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:
        logger.exception("Chat Claude API call failed")
        return f"Sorry, I could not reach the analysis service right now. ({exc})"

    reply = ""
    for block in response.content:
        if block.type == "text":
            reply += block.text

    return reply.strip()[:3500] if reply.strip() else "I don't have an answer for that right now."
