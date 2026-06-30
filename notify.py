"""Telegram delivery: per-subscriber DMs with chart, rationale, and PnL footer."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Bot

import access
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


async def send_photo_with_caption(
    bot: Bot,
    chat_id: int | str,
    chart_path: str,
    caption: str,
) -> None:
    """Send a chart image with caption to a chat."""
    path = Path(chart_path)
    if not path.exists():
        raise FileNotFoundError(f"Chart not found: {chart_path}")

    with open(path, "rb") as photo:
        await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption[:1024])


async def send_suggestion_to_chat(
    bot: Bot,
    chat_id: int | str,
    suggestion: Suggestion,
    chart_paths: list[str] | str,
    pnl_footer: str,
) -> None:
    paths = [chart_paths] if isinstance(chart_paths, str) else list(chart_paths[:2])
    if not paths:
        raise FileNotFoundError("No chart paths provided")

    caption = build_caption(suggestion)
    rationale_message = build_rationale_message(suggestion, pnl_footer)

    for i, chart_path in enumerate(paths):
        path = Path(chart_path)
        if not path.exists():
            raise FileNotFoundError(f"Chart not found: {chart_path}")

        photo_caption = caption if i == 0 else f"Chart {i + 1}/{len(paths)}"
        try:
            with open(path, "rb") as photo:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=photo_caption[:1024])
        except Exception:
            logger.exception(
                "Photo send failed for chat %s (%s), skipping chart",
                chat_id,
                path.name,
            )
            continue

    if rationale_message:
        await bot.send_message(chat_id=chat_id, text=rationale_message)


async def send_research_to_chat(
    bot: Bot,
    chat_id: int | str,
    chart_path: str,
    caption: str,
    detail_text: str,
) -> None:
    """Send research chart + follow-up detail message."""
    await send_photo_with_caption(bot, chat_id, chart_path, caption)
    if detail_text:
        await bot.send_message(chat_id=chat_id, text=detail_text[:4096])


async def broadcast_to_subscribers(
    bot: Bot,
    suggestion: Suggestion,
    chart_paths: list[str] | str,
    pnl_footer: str | None = None,
) -> None:
    """DM the suggestion to every registered subscriber (or allowlist if paywall on)."""
    footer = pnl_footer or paper.format_pnl_footer()
    recipients = access.broadcast_recipient_ids()
    sent: set[int] = set()

    for user_id in recipients:
        if user_id in sent:
            continue
        try:
            await send_suggestion_to_chat(bot, user_id, suggestion, chart_paths, footer)
            sent.add(user_id)
            logger.info("Sent suggestion to user %s", user_id)
        except Exception:
            logger.exception("Failed to send to user %s", user_id)

    admin_chat = config.TELEGRAM_ADMIN_CHAT_ID or config.TELEGRAM_CHAT_ID
    if admin_chat:
        try:
            admin_id = int(str(admin_chat).strip())
        except ValueError:
            admin_id = None
        if admin_id is not None and admin_id not in sent:
            try:
                await send_suggestion_to_chat(bot, admin_chat, suggestion, chart_paths, footer)
                logger.info("Sent suggestion to admin chat %s", admin_chat)
            except Exception:
                logger.exception("Failed to send to admin chat %s", admin_chat)


def broadcast(
    suggestion: Suggestion,
    chart_paths: list[str] | str,
    pnl_footer: str | None = None,
) -> None:
    """Sync wrapper for standalone agent.py / tests."""
    footer = pnl_footer or paper.format_pnl_footer()

    async def _run() -> None:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await broadcast_to_subscribers(bot, suggestion, chart_paths, footer)

    asyncio.run(_run())


def _latest_output_chart() -> Path:
    for pattern in ("*_entry.png", "*_structure.png", "*_notrade.png", "*_H1_annotated.png"):
        charts_found = sorted(config.CHARTS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
        if charts_found:
            return charts_found[-1]
    raise FileNotFoundError("No output charts in charts/. Run agent.py first.")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    chart = Path(sys.argv[1]) if len(sys.argv) > 1 else _latest_output_chart()
    suggestion = Suggestion.no_trade(
        rationale="Notify checkpoint — test broadcast to allowlisted users.",
    )

    print(f"Broadcasting {chart} ...")
    broadcast(suggestion, str(chart))
    print("Done. Check Telegram DMs.")
