"""Telegram bot handlers — access gate, status, chat Q&A, and research."""

from __future__ import annotations

import asyncio
import logging
import re

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import access
import analytics
import chart_view
import chat
import config
import critic
import ledger
import notify
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
    "\"What would invalidate the trade?\" Use /chart to see the latest analysis chart.\n\n"
    "Research: ask about historical patterns, e.g. \"What % of H12 SFPs reversed "
    "in the past 4 years?\" or use /research h12_sfp\n\n"
    "Paper PnL assumes a ${start:,.0f} portfolio with 1% risk per trade. Not financial advice."
)

_RESEARCH_KEYWORDS = re.compile(
    r"(weekly\s+sfp|h12\s+sfp|sfp\s+reversal|%.*sfp|sfp.*%|sfp.*past|past.*sfp|"
    r"research\s+(weekly|h12)|how\s+many\s+sfp)",
    re.IGNORECASE,
)

_CHART_QUERY = re.compile(
    r"(?:"
    r"show\s+(?:me\s+)?(?:the\s+)?(?:latest\s+)?charts?"
    r"|send\s+(?:me\s+)?(?:the\s+)?charts?"
    r"|(?:latest|current)\s+charts?"
    r"|what(?:'s|\s+is)\s+(?:on\s+the\s+chart|the\s+bot\s+watching|are\s+you\s+watching)"
    r"|what\s+are\s+you\s+watching"
    r"|show\s+(?:me\s+)?what(?:'s|\s+you(?:'re|\s+are))\s+watching"
    r"|what\s+(?:chart|charts)\s+(?:are\s+you|is\s+the\s+bot)\s+using"
    r")",
    re.IGNORECASE,
)


def _username(update: Update) -> str | None:
    user = update.effective_user
    if user is None:
        return None
    return user.username


def _is_research_query(text: str) -> bool:
    normalized = text.strip().lower()
    if normalized.startswith("/research"):
        return True
    return bool(_RESEARCH_KEYWORDS.search(text))


def _is_chart_query(text: str) -> bool:
    normalized = text.strip().lower()
    if normalized in ("/chart", "chart"):
        return True
    return bool(_CHART_QUERY.search(text))


def _research_years(text: str) -> int:
    match = re.search(r"(\d+)\s*years?", text, re.IGNORECASE)
    if match:
        return max(1, min(int(match.group(1)), 10))
    return 4


async def _reply(update: Update, text: str) -> None:
    if update.message is None:
        return
    await update.message.reply_text(text)


def _research_timeframe(text: str) -> str:
    normalized = text.strip().lower()
    if "h12" in normalized or "12h" in normalized or "12-hour" in normalized:
        return "H12"
    return "W1"


async def _handle_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.chat.send_action("upload_photo")
    loop = asyncio.get_running_loop()
    try:
        view = await loop.run_in_executor(None, chart_view.get_latest_chart_view)
    except Exception:
        logger.exception("Chart handler failed")
        await _reply(update, "Sorry, I could not load the latest chart right now.")
        return

    if view is None:
        await _reply(
            update,
            "No chart yet. The agent runs every hour — check back after the first cycle.",
        )
        return

    bot = context.bot
    chat_id = update.effective_chat.id if update.effective_chat else update.message.chat_id
    try:
        for i, chart_path in enumerate(view.chart_paths):
            caption = view.caption if i == 0 else f"Chart {i + 1}/{len(view.chart_paths)}"
            await notify.send_photo_with_caption(bot, chat_id, chart_path, caption)
    except Exception:
        logger.exception("Failed to send chart photo")
        await _reply(update, "Sorry, I could not send the chart image right now.")
        return

    spot = research.get_spot_price()
    pnl = paper.format_pnl_footer(spot)
    await _reply(update, f"{view.watch_summary}\n\n{pnl}"[:4096])


async def _handle_research(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if update.message is None:
        return

    years = _research_years(text)
    timeframe = _research_timeframe(text)
    label = "H12" if timeframe == "H12" else "weekly"
    await _reply(update, f"Analyzing {label} SFPs over the past {years} years...")

    loop = asyncio.get_running_loop()
    try:
        if timeframe == "H12":
            result = await loop.run_in_executor(None, analytics.h12_sfp_report, years)
        else:
            result = await loop.run_in_executor(None, analytics.weekly_sfp_report, years)
    except Exception:
        logger.exception("Research handler failed")
        await _reply(update, "Sorry, the research analysis failed. Try again later.")
        return

    bot = context.bot
    chat_id = update.effective_chat.id if update.effective_chat else update.message.chat_id
    try:
        await notify.send_research_to_chat(
            bot,
            chat_id,
            result.chart_path,
            result.caption,
            result.summary_text,
        )
    except Exception:
        logger.exception("Failed to send research chart")
        await _reply(update, result.summary_text[:4096])


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
    position_detail = paper.format_position_detail(spot)
    latest = ledger.get_latest_trade_suggestion() or ledger.get_latest_suggestion()

    lines = [welcome, ""]
    if position_detail:
        lines.append(position_detail)
        lines.append("")
    elif latest:
        lines.append(f"Latest: {latest['action']} @ cycle {latest['cycle_id']}")
        if latest.get("rationale"):
            rationale = notify.format_rationale_text(str(latest["rationale"]))
            max_len = 500
            if len(rationale) > max_len:
                rationale = rationale[:max_len].rstrip() + "..."
            lines.append(rationale)
        lines.append("")
    closed_detail = paper.format_closed_trades_detail()
    if closed_detail:
        lines.append(closed_detail)
        lines.append("")
    lines.append(pnl)

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
        "/chart — latest analysis chart + what the bot is watching\n"
        "/research weekly_sfp — weekly SFP reversal study (4 years)\n"
        "/research h12_sfp — H12 SFP reversal study (4 years)\n"
        "/help — this message\n\n"
        "Ask about the latest hourly suggestion, e.g.:\n"
        "• Why this entry?\n"
        "• What invalidates the trade?\n"
        "• How does this match the SFP example?\n"
        "• /chart or \"show me the latest chart\"\n\n"
        "Research questions (returns chart + stats), e.g.:\n"
        "• What % of H12 SFPs resulted in a reversal in the past 4 years?\n"
        "• /research h12_sfp\n"
        "• /research weekly_sfp",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    spot = research.get_spot_price()
    pnl = paper.format_pnl_footer(spot)
    position_detail = paper.format_position_detail(spot)

    latest = ledger.get_latest_suggestion()
    if position_detail:
        lines = [position_detail]
        if latest:
            open_positions = paper.get_open_positions(spot)
            open_cids = {
                str(p["open_cycle_id"])
                for p in open_positions
                if p.get("open_cycle_id")
            }
            header = "Latest hourly cycle"
            if open_cids and latest.get("cycle_id") not in open_cids:
                header += " (may differ from open positions)"
            tps = ", ".join(f"{tp:,.2f}" for tp in latest.get("take_profits", [])) or "n/a"
            lines.extend(
                [
                    "",
                    f"--- {header} ---",
                    f"Cycle: {latest['cycle_id']} ({latest['ts']})",
                    f"Action: {latest['action']}",
                    f"Entry: {latest.get('entry')} | SL: {latest.get('stop_loss')} | TP: {tps}",
                    f"R/R: {latest.get('risk_reward')}",
                ]
            )
            rationale = notify.format_rationale_text(str(latest.get("rationale", "")))
            if rationale:
                max_len = 600
                if len(rationale) > max_len:
                    rationale = rationale[:max_len].rstrip() + "..."
                lines.extend(["", rationale])
        closed_detail = paper.format_closed_trades_detail()
        if closed_detail:
            lines.extend(["", closed_detail])
        lines.extend(["", pnl])
        await _reply(update, "\n".join(lines)[:4096])
        return

    latest = ledger.get_latest_trade_suggestion() or latest
    if latest is None:
        closed_detail = paper.format_closed_trades_detail()
        body = f"No suggestions yet."
        if closed_detail:
            body += f"\n\n{closed_detail}"
        await _reply(update, f"{body}\n\n{pnl}")
        return

    tps = ", ".join(f"{tp:,.2f}" for tp in latest.get("take_profits", [])) or "n/a"
    body = (
        f"Cycle: {latest['cycle_id']}\n"
        f"Action: {latest['action']}\n"
        f"Entry: {latest.get('entry')}\n"
        f"SL: {latest.get('stop_loss')}\n"
        f"TP: {tps}\n"
        f"R/R: {latest.get('risk_reward')}\n\n"
        f"Rationale:\n{notify.format_rationale_text(str(latest.get('rationale', '')))}\n"
    )
    closed_detail = paper.format_closed_trades_detail()
    if closed_detail:
        body += f"\n{closed_detail}\n"
    body += f"\n{pnl}"
    await _reply(update, body[:4096])


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    await _handle_chart(update, context)


async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    args = context.args or []
    subcmd = args[0].lower() if args else "h12_sfp"
    if subcmd in ("weekly_sfp", "weekly-sfp", "weekly"):
        timeframe = "W1"
    elif subcmd in ("h12_sfp", "h12-sfp", "h12", "sfp"):
        timeframe = "H12"
    else:
        await _reply(
            update,
            "Usage:\n"
            "/research h12_sfp — H12 SFP study (default)\n"
            "/research weekly_sfp — weekly SFP study\n\n"
            "Or ask in plain text, e.g. \"What % of H12 SFPs reversed in the past 4 years?\"",
        )
        return

    years = 4
    for arg in args[1:]:
        match = re.search(r"(\d+)", arg)
        if match:
            years = max(1, min(int(match.group(1)), 10))
            break

    prefix = "h12" if timeframe == "H12" else "weekly"
    text = f"{prefix} sfp past {years} years"
    await _handle_research(update, context, text)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None or not update.message.text:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    user_text = update.message.text.strip()

    if _is_research_query(user_text):
        await _handle_research(update, context, user_text)
        return

    if _is_chart_query(user_text):
        await _handle_chart(update, context)
        return

    await update.message.chat.send_action("typing")

    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(None, chat.answer, user_text)
    except Exception:
        logger.exception("Chat handler failed")
        reply = "Sorry, something went wrong processing your message."

    try:
        latest = ledger.get_latest_suggestion()
        cycle_id = str(latest["cycle_id"]) if latest else None

        def _refine_chat() -> tuple[str, object]:
            return critic.refine_chat_reply(
                user.id,
                user_text,
                reply,
                cycle_id=cycle_id,
            )

        reply, verdict = await loop.run_in_executor(None, _refine_chat)
        if verdict.has_issues:
            await loop.run_in_executor(None, notify.send_monitor_alert, verdict)
    except Exception:
        logger.exception("Chat monitor audit failed")

    spot = research.get_spot_price()
    pnl = paper.format_pnl_footer(spot)
    await _reply(update, f"{reply}\n\n{pnl}"[:4096])


def _is_macro_admin(user_id: int) -> bool:
    admin = config.TELEGRAM_ADMIN_CHAT_ID or config.MONITOR_CHAT_ID
    if admin and str(user_id) == str(admin).strip():
        return True
    return False


async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually ingest a headline for macro classification (admin/monitor only)."""
    user = update.effective_user
    if user is None or update.message is None:
        return

    if not _is_macro_admin(user.id):
        await _reply(update, "Macro ingest is restricted to the monitor/admin account.")
        return

    args = context.args or []
    if not args:
        await _reply(
            update,
            "Usage: /macro <headline text>\n"
            "Or: /macro <url>\n\n"
            "Forces LLM classification (bypasses keyword promote threshold).",
        )
        return

    text = " ".join(args).strip()
    url = text if text.startswith("http") else None
    title = text

    loop = asyncio.get_running_loop()
    try:
        from macro.ingest import ingest_headline

        event = await loop.run_in_executor(
            None,
            lambda: ingest_headline(
                title=title,
                url=url,
                source="telegram",
                force_classify=True,
            ),
        )
    except Exception:
        logger.exception("Macro command failed")
        await _reply(update, "Macro ingest failed.")
        return

    if event is None:
        await _reply(update, "Duplicate or disabled — no new event stored.")
        return

    sev = event.get("severity", 0)
    bias = event.get("eth_bias") or "n/a"
    kscore = event.get("keyword_score", 0)
    await _reply(
        update,
        f"Macro ingested (id={event.get('id')})\n"
        f"keyword_score={kscore} | severity={sev} | bias={bias}\n"
        f"status={event.get('status')}",
    )


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("macro", cmd_macro))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
