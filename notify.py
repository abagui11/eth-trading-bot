"""One-way Telegram broadcast: chart image + suggestion caption."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Bot

import config
from models import Suggestion

logger = logging.getLogger(__name__)

# TODO: inline approve/reject buttons + APPROVAL_WINDOW_MIN timeout (full build).


def build_caption(suggestion: Suggestion) -> str:
    """Short caption for the chart photo (Telegram limit: 1024 characters)."""
    if suggestion.action == "no_trade":
        return "NO TRADE — rationale in the message below."

    tps = ", ".join(f"{tp:,.2f}" for tp in suggestion.take_profits[:3]) or "n/a"
    rr = f"{suggestion.risk_reward:.2f}" if suggestion.risk_reward is not None else "n/a"
    return (
        f"{suggestion.action.upper()}\n"
        f"Entry: {suggestion.entry:,.2f}\n"
        f"SL: {suggestion.stop_loss:,.2f}\n"
        f"TP: {tps}\n"
        f"R/R: {rr}\n"
        f"Size: {suggestion.size}"
    )


def build_rationale_message(suggestion: Suggestion) -> str:
    """Full rationale as a follow-up text message (Telegram limit: 4096 characters)."""
    if not suggestion.rationale.strip():
        return ""
    header = "NO TRADE" if suggestion.action == "no_trade" else suggestion.action.upper()
    text = f"{header}\n\nRationale:\n{suggestion.rationale.strip()}"
    return text[:4096]


async def _send_photo(chart_path: str, caption: str, rationale_message: str) -> None:
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    with open(chart_path, "rb") as photo:
        await bot.send_photo(
            chat_id=config.TELEGRAM_CHAT_ID,
            photo=photo,
            caption=caption,
        )
    if rationale_message:
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=rationale_message,
        )


def broadcast(suggestion: Suggestion, marked_up_chart_path: str) -> None:
    """Post the marked-up chart and caption to the configured Telegram channel."""
    path = Path(marked_up_chart_path)
    if not path.exists():
        raise FileNotFoundError(f"Chart not found: {marked_up_chart_path}")

    caption = build_caption(suggestion)
    rationale_message = build_rationale_message(suggestion)
    asyncio.run(_send_photo(str(path), caption, rationale_message))
    logger.info("Broadcast sent to chat %s", config.TELEGRAM_CHAT_ID)


def _latest_annotated_chart() -> Path:
    charts = sorted(config.CHARTS_DIR.glob("*_H1_annotated.png"), key=lambda p: p.stat().st_mtime)
    if not charts:
        raise FileNotFoundError("No annotated charts in charts/. Run analyze.py first.")
    return charts[-1]


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    chart = Path(sys.argv[1]) if len(sys.argv) > 1 else _latest_annotated_chart()
    suggestion = Suggestion.no_trade(
        rationale=(
            "W1 structure is strongly bearish: series of lower highs and lower lows from "
            "~4800 peak in Aug 2025 down to current ~1650 area. D1 confirms breakdown from "
            "~2000 support. H4 shows lower high near 1780 on Jun 22 with sharp rejection. "
            "H1 confirms aggressive selloff to ~1650 with no base-building. No trade this cycle."
        ),
    )

    print(f"Broadcasting {chart} ...")
    broadcast(suggestion, str(chart))
    print("Done. Check your Telegram channel.")
