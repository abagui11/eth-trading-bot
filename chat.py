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
- The agent's ICT swing strategy and Trading Guide
- The current open paper position (including SL, TP, entry, size, unrealized P&L, exit plan)
- The current or latest hourly trade suggestion
- Paper portfolio performance shown in the PnL line

When a position is open, always reference its stop loss, take profit levels, and exit plan
from the open position block — do not say SL/TP are missing if they appear there.

If the context includes a "Latest hourly cycle" section that differs from the open position,
explain both: what is live in paper vs what the most recent hourly analysis recommended.

Be concise and practical. For historical pattern research (e.g. weekly or H12 SFP stats over past years),
tell the user to ask directly or use /research h12_sfp or /research weekly_sfp — separate analysis with charts.
This is not financial advice.
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
        f"  setup_tags: {row.get('setup_tags') or 'n/a'}\n"
        f"  chart_path: {row.get('chart_path')}"
    )


def _format_latest_cycle_summary(latest: dict, open_cycle_id: str | None) -> str:
    header = "=== Latest hourly cycle"
    if open_cycle_id and latest.get("cycle_id") != open_cycle_id:
        header += " (may differ from open position)"
    return f"{header} ===\n{_format_suggestion_context(latest)}"


def _pick_chart_path(*candidates: str | None) -> str | None:
    for path in candidates:
        if path and Path(path).exists():
            return path
        if path and "," in path:
            for part in path.split(","):
                part = part.strip()
                if part and Path(part).exists():
                    return part
    return None


def _build_context(spot: float) -> tuple[str, str | None]:
    """Return (text context, optional chart path for vision)."""
    parts: list[str] = [f"Current ETH spot: ${spot:,.2f}"]

    position_detail = paper.format_position_detail(spot)
    chart_path: str | None = None
    open_pos = paper.get_open_position(spot)
    open_cycle_id = str(open_pos["open_cycle_id"]) if open_pos and open_pos.get("open_cycle_id") else None
    latest = ledger.get_latest_suggestion()

    if position_detail:
        parts.append("")
        parts.append("=== Open paper position ===")
        parts.append(position_detail)

        if open_cycle_id:
            trade_row = ledger.get_suggestion_by_cycle_id(open_cycle_id)
            if trade_row:
                parts.append("")
                parts.append("Open position rationale:")
                parts.append(str(trade_row.get("rationale", "")).strip())
                chart_path = _pick_chart_path(trade_row.get("chart_path"))

        if latest:
            parts.append("")
            parts.append(_format_latest_cycle_summary(latest, open_cycle_id))
            chart_path = _pick_chart_path(latest.get("chart_path"), chart_path)
    elif latest:
        parts.append("")
        parts.append(_format_suggestion_context(latest))
        chart_path = _pick_chart_path(latest.get("chart_path"))
    else:
        trade = ledger.get_latest_trade_suggestion()
        if trade:
            parts.append("")
            parts.append(_format_suggestion_context(trade))
            chart_path = _pick_chart_path(trade.get("chart_path"))

    parts.append("")
    parts.append(paper.format_pnl_footer(spot))
    return "\n".join(parts), chart_path


def answer(user_message: str) -> str:
    """Return Claude's reply about the latest suggestion (caller appends PnL footer)."""
    guide = analyze.load_trading_guide()
    spot = research.get_spot_price()

    if ledger.get_latest_suggestion() is None and not paper.is_open():
        return (
            "No trade suggestions yet. The agent runs every hour — check back after the first cycle."
        )

    text_context, chart_path = _build_context(spot)
    text_context = f"{text_context}\n\nUser question: {user_message}"

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
                    "text": guide + "\n\n" + _SYSTEM_SUFFIX,
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
