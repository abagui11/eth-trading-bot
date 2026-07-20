"""Telegram bot handlers — access gate, status, chat Q&A, and research."""

from __future__ import annotations

import asyncio
import logging
import re

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import access
import bot_config
import chart_view
import chat
import config
import critic
import ledger
import notify
import paper
import research
import telegram_ui
import user_books
from research_reports import catalog as research_catalog
from research_reports import router as research_router

logger = logging.getLogger(__name__)

PAYWALL_MESSAGE = (
    "Access required to receive hourly trade suggestions.\n\n"
    "Contact us to subscribe. Once approved, your Telegram ID will be added to the allowlist."
)

# Kept for any external imports; live copy lives in telegram_ui.
WELCOME_MESSAGE = telegram_ui.WELCOME_MESSAGE


def _is_research_query(text: str) -> bool:
    return research_catalog.is_research_query(text)


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


def _is_chart_query(text: str) -> bool:
    normalized = text.strip().lower()
    if normalized in ("/chart", "chart"):
        return True
    return bool(_CHART_QUERY.search(text))


async def _reply(update: Update, text: str) -> None:
    if update.message is None:
        return
    await update.message.reply_text(text)


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

    refuse = research_router.clarify_or_refuse(text)
    topic_id = research_router.resolve_topic(text)
    if topic_id is None:
        if refuse:
            await _reply(update, refuse)
            return
        await _reply(update, research_router.build_catalog())
        return

    years = research_router.parse_years(text)
    product_id = research_router.parse_product_id(text)
    status_msg = research_router.topic_status_message(topic_id)
    if status_msg:
        await _reply(update, status_msg)

    loop = asyncio.get_running_loop()
    try:
        report = await loop.run_in_executor(
            None,
            lambda: research_router.build_report(
                topic_id,
                years=years,
                text=text,
                product_id=product_id,
            ),
        )
    except Exception:
        logger.exception("Research handler failed for topic %s", topic_id)
        await _reply(update, "Sorry, the research analysis failed. Try again later.")
        return

    bot = context.bot
    chat_id = update.effective_chat.id if update.effective_chat else update.message.chat_id
    try:
        await notify.send_research_report(bot, chat_id, report)
    except Exception:
        logger.exception("Failed to send research report")
        await _reply(update, report.detail_text[:4096])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    spots = research.get_spot_prices()
    pnl = paper.format_pnl_footer(spots=spots)
    position_detail = paper.format_position_detail()
    latest = ledger.get_latest_trade_suggestion() or ledger.get_latest_suggestion()

    lines = [telegram_ui.WELCOME_MESSAGE, ""]
    if position_detail:
        lines.append(position_detail)
        lines.append("")
    elif latest:
        product = latest.get("product_id") or "ETH-USD"
        lines.append(
            f"Latest: {latest['action']} ({bot_config.product_label(product)}) "
            f"@ cycle {latest['cycle_id']}"
        )
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
    if config.DASHBOARD_PUBLIC_URL:
        lines.append("")
        lines.append(f"Portfolio dashboard: {config.DASHBOARD_PUBLIC_URL}")

    await update.message.reply_text(
        "\n".join(lines)[:4096],
        reply_markup=telegram_ui.main_keyboard(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    await query.answer()
    user_id = query.from_user.id
    access.register_user(user_id, query.from_user.username)
    if not access.is_allowed(user_id):
        await query.edit_message_text(PAYWALL_MESSAGE)
        return

    data = query.data or ""
    chat_id = query.message.chat_id if query.message else user_id

    if data == telegram_ui.CB_OPEN or data == telegram_ui.CB_FUND:
        if user_books.has_account(user_id):
            account = user_books.get_account(user_id)
            await context.bot.send_message(
                chat_id,
                telegram_ui.format_open_account_result(
                    {
                        "ok": False,
                        "reason": "already_opened",
                        "amount_usd": (account or {}).get("starting_usd"),
                        "cash_usd": (account or {}).get("cash_usd"),
                        "starting_usd": (account or {}).get("starting_usd"),
                    }
                ),
                reply_markup=telegram_ui.main_keyboard(),
            )
            return
        await context.bot.send_message(
            chat_id,
            telegram_ui.format_open_account_prompt(),
            reply_markup=telegram_ui.open_account_keyboard(),
        )
        return

    if data.startswith(telegram_ui.CB_OPEN_SIZE_PREFIX):
        raw = data[len(telegram_ui.CB_OPEN_SIZE_PREFIX) :]
        try:
            amount = float(raw)
        except ValueError:
            await context.bot.send_message(
                chat_id,
                "Invalid size.",
                reply_markup=telegram_ui.main_keyboard(),
            )
            return
        result = user_books.open_paper_account(
            user_id, amount, username=query.from_user.username
        )
        await context.bot.send_message(
            chat_id,
            telegram_ui.format_open_account_result(result),
            reply_markup=telegram_ui.main_keyboard(),
        )
        return

    if data == telegram_ui.CB_METRICS:
        spots = research.get_spot_prices()
        metrics = paper.get_user_metrics(user_id, spots=spots)
        await context.bot.send_message(
            chat_id,
            telegram_ui.format_metrics_message(metrics),
            reply_markup=telegram_ui.main_keyboard(),
        )
        return

    if data == telegram_ui.CB_MY_BOOK:
        url = user_books.me_url(user_id)
        if url:
            text = (
                "My book — personal demo ledger\n\n"
                f"Open your ledger: {url}\n"
                "(Link expires in about an hour; tap My book again for a fresh one.)"
            )
        else:
            text = (
                "My book needs DASHBOARD_PUBLIC_URL set on the server.\n"
                "Tap My Metrics for a text summary of your personal demo book."
            )
        await context.bot.send_message(
            chat_id,
            text,
            reply_markup=telegram_ui.main_keyboard(),
        )
        return

    if data.startswith(telegram_ui.CB_TRADE_YES_PREFIX):
        offer_id = data[len(telegram_ui.CB_TRADE_YES_PREFIX) :]
        spots = research.get_spot_prices()
        result = user_books.accept_offer(offer_id, user_id, spots=spots)
        if result.get("ok"):
            text = (
                f"Accepted.\n\n"
                f"Opened {result.get('side')} "
                f"{float(result.get('qty') or 0):.6f} @ "
                f"${float(result.get('entry') or 0):,.2f}\n"
                f"Notional: ${float(result.get('notional_usd') or 0):,.2f}\n"
                f"Cash left: ${float(result.get('cash_usd') or 0):,.2f}"
            )
        else:
            reason = result.get("reason") or "failed"
            if reason == "no_account":
                text = "Open a paper account first, then Accept."
            elif reason == "expired":
                text = (
                    "Accept window expired (15 min). "
                    "If the trade runs well you may get a missed-connection invite."
                )
            elif reason == "already_decided":
                text = f"Already recorded as {result.get('status')}."
            elif reason == "insufficient_cash":
                text = "Not enough demo cash to size this trade."
            else:
                text = f"Could not Accept ({reason})."
        await context.bot.send_message(
            chat_id, text, reply_markup=telegram_ui.main_keyboard()
        )
        return

    if data.startswith(telegram_ui.CB_TRADE_NO_PREFIX):
        offer_id = data[len(telegram_ui.CB_TRADE_NO_PREFIX) :]
        result = user_books.reject_offer(offer_id, user_id)
        if result.get("ok"):
            text = "Rejected — your demo cash stays out of this trade."
        elif result.get("reason") == "already_decided":
            text = f"Already recorded as {result.get('status')}."
        elif result.get("reason") == "no_account":
            text = "Open a paper account to track Accept/Reject on future cards."
        else:
            text = f"Could not Reject ({result.get('reason')})."
        await context.bot.send_message(
            chat_id, text, reply_markup=telegram_ui.main_keyboard()
        )
        return

    if data.startswith(telegram_ui.CB_TRADE_JOIN_PREFIX):
        offer_id = data[len(telegram_ui.CB_TRADE_JOIN_PREFIX) :]
        spots = research.get_spot_prices()
        offer = user_books.get_offer(offer_id)
        product = (offer or {}).get("product_id") or "ETH-USD"
        mark = float(spots.get(product) or 0)
        result = user_books.late_join_offer(
            offer_id, user_id, mark_price=mark, spots=spots
        )
        if result.get("ok"):
            text = (
                f"Joined at mark.\n\n"
                f"{result.get('side')} {float(result.get('qty') or 0):.6f} @ "
                f"${float(result.get('entry') or 0):,.2f}\n"
                f"Notional: ${float(result.get('notional_usd') or 0):,.2f}"
            )
        else:
            text = f"Could not join ({result.get('reason')})."
        await context.bot.send_message(
            chat_id, text, reply_markup=telegram_ui.main_keyboard()
        )
        return

    if data.startswith(telegram_ui.CB_TRADE_SKIP_PREFIX):
        offer_id = data[len(telegram_ui.CB_TRADE_SKIP_PREFIX) :]
        user_books.decline_missed_connection(offer_id, user_id)
        await context.bot.send_message(
            chat_id,
            "Okay — staying out of this trade.",
            reply_markup=telegram_ui.main_keyboard(),
        )
        return

    if data == telegram_ui.CB_RESEARCH:
        catalog = research_router.build_catalog()
        text = f"{telegram_ui.RESEARCH_HELP}\n\n{catalog}"
        await context.bot.send_message(
            chat_id,
            text[:4096],
            reply_markup=telegram_ui.main_keyboard(),
        )
        return

    if data == telegram_ui.CB_REFRESH:
        spots = research.get_spot_prices()
        pnl = paper.format_pnl_footer(spots=spots)
        text = f"{telegram_ui.WELCOME_MESSAGE}\n\n{pnl}"
        if config.DASHBOARD_PUBLIC_URL:
            text += f"\n\nAgent journal: {config.DASHBOARD_PUBLIC_URL}"
        await context.bot.send_message(
            chat_id,
            text[:4096],
            reply_markup=telegram_ui.main_keyboard(),
        )
        return


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    await update.message.reply_text(
        "Commands:\n"
        "/start — welcome + menu (Open account, My Metrics, My book, Journal, Research)\n"
        "/status — current suggestion + paper PnL\n"
        "/chart — latest analysis chart + what the bot is watching\n"
        "/research — research topic catalog\n"
        "/help — this message\n\n"
        + research_router.build_catalog(),
        reply_markup=telegram_ui.main_keyboard(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    spots = research.get_spot_prices()
    pnl = paper.format_pnl_footer(spots=spots)
    position_detail = paper.format_position_detail()

    latest = ledger.get_latest_suggestion()
    if position_detail:
        lines = [position_detail]
        if latest:
            open_positions = paper.get_open_positions(spots=spots)
            open_cids = {
                str(p["open_cycle_id"])
                for p in open_positions
                if p.get("open_cycle_id")
            }
            header = "Latest hourly cycle"
            if open_cids and latest.get("cycle_id") not in open_cids:
                header += " (may differ from open positions)"
            product = latest.get("product_id") or "ETH-USD"
            tps = ", ".join(f"{tp:,.2f}" for tp in latest.get("take_profits", [])) or "n/a"
            lines.extend(
                [
                    "",
                    f"--- {header} ---",
                    f"Cycle: {latest['cycle_id']} ({latest['ts']})",
                    f"Asset: {bot_config.product_label(product)}",
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
    if not args:
        await _reply(update, research_router.build_catalog())
        return

    subcmd = args[0].lower()
    topic_id = research_catalog.topic_from_token(subcmd)
    if topic_id is None:
        await _reply(
            update,
            f"Unknown topic: {subcmd}\n\n{research_router.build_catalog()}",
        )
        return

    years = 4
    product_parts: list[str] = []
    for arg in args[1:]:
        match = re.search(r"(\d+)", arg)
        if match and years == 4 and not re.fullmatch(r"(?i)eth|btc|eth-usd|btc-usd", arg):
            years = max(1, min(int(match.group(1)), 10))
        product_parts.append(arg)

    product_hint = " ".join(product_parts)
    product_id = research_router.parse_product_id(product_hint)
    text = f"/research {topic_id} {years} years {product_id}"
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
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
