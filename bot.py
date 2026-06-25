"""Telegram bot handlers — access gate, status, and chat Q&A."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import access
import chat
import config
import ledger
import paper
import research

logger = logging.getLogger(__name__)

PAYWALL_MESSAGE = (
    "Access required to receive hourly ETH trade suggestions.\n\n"
    "Contact us to subscribe. Once approved, your Telegram ID will be added to the allowlist."
)

WELCOME_MESSAGE = (
    "Welcome to the ETH Trading Agent.\n\n"
    "You will receive an hourly trade suggestion (chart + rationale) if a setup is found.\n"
    "Reply anytime to ask about the latest suggestion — e.g. \"Why this entry?\" or "
    "\"What would invalidate the trade?\"\n\n"
    "Paper PnL assumes a ${start:,.0f} portfolio with 1% risk per trade. Not financial advice."
)


def _username(update: Update) -> str | None:
    user = update.effective_user
    if user is None:
        return None
    return user.username


async def _reply(update: Update, text: str) -> None:
    if update.message is None:
        return
    await update.message.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    welcome = WELCOME_MESSAGE.format(start=config.PAPER_PORTFOLIO_VALUE)
    spot = research.get_spot_price()
    pnl = paper.format_pnl_footer(spot)
    latest = ledger.get_latest_suggestion()

    lines = [welcome, "", pnl]
    if latest:
        lines.append("")
        lines.append(f"Latest: {latest['action']} @ cycle {latest['cycle_id']}")
        if latest.get("rationale"):
            snippet = str(latest["rationale"])[:300]
            lines.append(snippet)

    await _reply(update, "\n".join(lines)[:4096])


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    await _reply(
        update,
        "Commands:\n"
        "/start — welcome + latest status\n"
        "/status — current suggestion + paper PnL\n"
        "/help — this message\n\n"
        "Ask anything about the latest hourly suggestion, e.g.:\n"
        "• Why this entry?\n"
        "• What invalidates the trade?\n"
        "• How does this match the SFP example?",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    latest = ledger.get_latest_suggestion()
    spot = research.get_spot_price()
    pnl = paper.format_pnl_footer(spot)

    if latest is None:
        await _reply(update, f"No suggestions yet.\n\n{pnl}")
        return

    tps = ", ".join(f"{tp:,.2f}" for tp in latest.get("take_profits", [])) or "n/a"
    body = (
        f"Cycle: {latest['cycle_id']}\n"
        f"Action: {latest['action']}\n"
        f"Entry: {latest.get('entry')}\n"
        f"SL: {latest.get('stop_loss')}\n"
        f"TP: {tps}\n"
        f"R/R: {latest.get('risk_reward')}\n\n"
        f"Rationale:\n{latest.get('rationale', '')}\n\n"
        f"{pnl}"
    )
    await _reply(update, body[:4096])


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None or not update.message.text:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    await update.message.chat.send_action("typing")

    user_text = update.message.text.strip()
    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(None, chat.answer, user_text)
    except Exception:
        logger.exception("Chat handler failed")
        reply = "Sorry, something went wrong processing your message."

    spot = research.get_spot_price()
    pnl = paper.format_pnl_footer(spot)
    await _reply(update, f"{reply}\n\n{pnl}"[:4096])


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
