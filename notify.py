"""Telegram delivery: per-subscriber DMs with chart, rationale, and PnL footer."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Bot

import config
import paper
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


def build_rationale_message(suggestion: Suggestion, pnl_footer: str) -> str:
    """Full rationale + PnL as a follow-up text message."""
    parts: list[str] = []
    if suggestion.rationale.strip():
        header = "NO TRADE" if suggestion.action == "no_trade" else suggestion.action.upper()
        parts.append(f"{header}\n\nRationale:\n{suggestion.rationale.strip()}")
    parts.append(pnl_footer)
    return "\n\n".join(parts)[:4096]


async def send_suggestion_to_chat(
    bot: Bot,
    chat_id: int | str,
    suggestion: Suggestion,
    chart_path: str,
    pnl_footer: str,
) -> None:
    path = Path(chart_path)
    if not path.exists():
        raise FileNotFoundError(f"Chart not found: {chart_path}")

    caption = build_caption(suggestion)
    rationale_message = build_rationale_message(suggestion, pnl_footer)

    with open(path, "rb") as photo:
        await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
    if rationale_message:
        await bot.send_message(chat_id=chat_id, text=rationale_message)


async def broadcast_to_subscribers(
    bot: Bot,
    suggestion: Suggestion,
    chart_path: str,
    pnl_footer: str | None = None,
) -> None:
    """DM the suggestion to every allowlisted Telegram user."""
    footer = pnl_footer or paper.format_pnl_footer()
    for user_id in config.ALLOWED_TELEGRAM_IDS:
        try:
            await send_suggestion_to_chat(bot, user_id, suggestion, chart_path, footer)
            logger.info("Sent suggestion to user %s", user_id)
        except Exception:
            logger.exception("Failed to send to user %s", user_id)

    allowed = set(config.ALLOWED_TELEGRAM_IDS)
    admin_chat = config.TELEGRAM_ADMIN_CHAT_ID or config.TELEGRAM_CHAT_ID
    if admin_chat:
        try:
            admin_id = int(str(admin_chat).strip())
        except ValueError:
            admin_id = None
        if admin_id is not None and admin_id not in allowed:
            try:
                await send_suggestion_to_chat(bot, admin_chat, suggestion, chart_path, footer)
                logger.info("Sent suggestion to admin chat %s", admin_chat)
            except Exception:
                logger.exception("Failed to send to admin chat %s", admin_chat)


def broadcast(suggestion: Suggestion, marked_up_chart_path: str, pnl_footer: str | None = None) -> None:
    """Sync wrapper for standalone agent.py / tests."""
    footer = pnl_footer or paper.format_pnl_footer()

    async def _run() -> None:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await broadcast_to_subscribers(bot, suggestion, marked_up_chart_path, footer)

    asyncio.run(_run())


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
        rationale="Notify checkpoint — test broadcast to allowlisted users.",
    )

    print(f"Broadcasting {chart} ...")
    broadcast(suggestion, str(chart))
    print("Done. Check Telegram DMs.")
