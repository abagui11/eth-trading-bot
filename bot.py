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
import trade_ideas_bridge
import user_books
from research_reports import catalog as research_catalog
from research_reports import router as research_router

logger = logging.getLogger(__name__)

PAYWALL_MESSAGE = (
    "Access required to receive hourly trade suggestions.\n\n"
    "Contact us to subscribe. Once approved, your Telegram ID will be added to the allowlist."
)

# Callback prefix for trade_ideas mill cards (idea:accept:<id> / idea:reject:<id>).
_CB_IDEA_PREFIX = "idea:"
# Personal idea-portfolio closes from /me (uportfolio:close:<user_paper_id>).
_CB_UPORTFOLIO_PREFIX = "uportfolio:"

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

# Volume-lane idea book (trade_ideas mill paper book) — not personal demo books.
_PERFORMANCE_QUERY = re.compile(
    r"(?:"
    r"/performance\b"
    r"|/ideas\b"
    r"|\bperformance\b"
    r"|\bidea\s+book\b"
    r"|\bvolume\s+book\b"
    r"|\bhow\s+are\s+(?:the\s+)?ideas?\s+doing\b"
    r"|\bhow(?:'s|\s+is)\s+(?:the\s+)?(?:idea\s+)?book\b"
    r"|\bshow\s+(?:me\s+)?(?:the\s+)?(?:idea\s+)?performance\b"
    r")",
    re.IGNORECASE,
)

# Personal accepted-idea portfolio (trade_ideas user_paper_trades) — not demo cash book.
_ME_QUERY = re.compile(
    r"(?:"
    r"/me\b"
    r"|\bmy\s+(?:idea\s+)?(?:portfolio|pnl|book)\b"
    r"|\bpersonal\s+(?:idea\s+)?(?:portfolio|pnl|book)\b"
    r"|\bhow\s+am\s+i\s+doing\b"
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


def _is_performance_query(text: str) -> bool:
    normalized = text.strip().lower()
    if normalized in ("/performance", "performance", "/ideas", "ideas"):
        return True
    return bool(_PERFORMANCE_QUERY.search(text))


def _is_me_query(text: str) -> bool:
    normalized = text.strip().lower()
    if normalized in ("/me", "me"):
        return True
    return bool(_ME_QUERY.search(text))


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


async def _handle_performance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Volume-lane idea book (trade_ideas mill)."""
    if update.message is None:
        return

    await update.message.chat.send_action("typing")
    loop = asyncio.get_running_loop()

    def _load() -> str:
        spots = research.get_spot_prices()
        report = trade_ideas_bridge.volume_book_report(spots)
        return trade_ideas_bridge.format_volume_book_report(report)

    try:
        text = await loop.run_in_executor(None, _load)
    except Exception:
        logger.exception("Performance handler failed")
        await _reply(update, "Sorry, I could not load idea-book performance right now.")
        return
    await _reply(update, text[:4096])


async def _handle_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Personal accepted-idea portfolio (trade_ideas user_paper_trades)."""
    if update.message is None or update.effective_user is None:
        return

    user_id = update.effective_user.id
    await update.message.chat.send_action("typing")
    loop = asyncio.get_running_loop()

    def _load() -> tuple[str, object]:
        spots = research.get_spot_prices()
        report = trade_ideas_bridge.user_book_report(user_id, spots)
        text = trade_ideas_bridge.format_user_book_report(report)
        keyboard = trade_ideas_bridge.user_book_close_keyboard(report)
        return text, keyboard

    try:
        text, keyboard = await loop.run_in_executor(None, _load)
    except Exception:
        logger.exception("Me handler failed")
        await _reply(update, "Sorry, I could not load your idea portfolio right now.")
        return
    await update.message.reply_text(text[:4096], reply_markup=keyboard)


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

    # Personal idea portfolio: close open trade at spot from /me buttons.
    if data.startswith(_CB_UPORTFOLIO_PREFIX):
        parts = data.split(":")
        if len(parts) != 3 or parts[1] != "close":
            return
        try:
            paper_id = int(parts[2])
        except ValueError:
            return
        loop = asyncio.get_running_loop()

        def _close_proper() -> tuple[str, dict | None]:
            trade = trade_ideas_bridge.get_open_user_trade(user_id, paper_id)
            if trade is None:
                # Distinguish missing DB vs missing row
                if not trade_ideas_bridge.enabled():
                    return "unavailable", None
                return "not_found", None
            product = str(trade.get("product_id") or "")
            spots = research.get_spot_prices()
            spot = spots.get(product)
            if spot is None:
                return "no_spot", None
            status, closed = trade_ideas_bridge.close_user_trade_at_spot(
                user_id, paper_id, spot
            )
            return status, closed

        try:
            status, trade = await loop.run_in_executor(None, _close_proper)
        except Exception:
            logger.exception("uportfolio close failed")
            await context.bot.send_message(
                chat_id,
                trade_ideas_bridge.format_close_reply("unavailable"),
                reply_markup=telegram_ui.main_keyboard(),
            )
            return
        await context.bot.send_message(
            chat_id,
            trade_ideas_bridge.format_close_reply(status, trade),
            reply_markup=telegram_ui.main_keyboard(),
        )
        if status == "closed":

            def _refresh() -> tuple[str, object]:
                spots = research.get_spot_prices()
                report = trade_ideas_bridge.user_book_report(user_id, spots)
                return (
                    trade_ideas_bridge.format_user_book_report(report),
                    trade_ideas_bridge.user_book_close_keyboard(report),
                )

            try:
                text, keyboard = await loop.run_in_executor(None, _refresh)
                await context.bot.send_message(
                    chat_id, text[:4096], reply_markup=keyboard
                )
            except Exception:
                logger.exception("uportfolio refresh after close failed")
        return

    # Volume-lane idea cards are sent by the trade_ideas mill through this
    # bot's token; this process owns the update stream, so their Accept/Reject
    # callbacks are recorded here.
    if data.startswith(_CB_IDEA_PREFIX):
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in ("accept", "reject"):
            return
        decision = parts[1]
        try:
            idea_id = int(parts[2])
        except ValueError:
            return
        status = trade_ideas_bridge.record_decision(idea_id, user_id, decision)
        await context.bot.send_message(
            chat_id,
            trade_ideas_bridge.format_decision_reply(status, decision, idea_id),
            reply_markup=telegram_ui.main_keyboard(),
        )
        # An Accept from a fill operator also takes a real mill clip. Runs off
        # the event loop (SQLite + Coinbase REST) and never blocks the Accept
        # itself — a full sleeve reports back instead of filling.
        if (
            decision == "accept"
            and status == "recorded"
            and trade_ideas_bridge.is_fill_operator(user_id)
        ):
            loop = asyncio.get_running_loop()
            try:
                verdict = await loop.run_in_executor(
                    None, trade_ideas_bridge.request_manual_fill, idea_id, user_id
                )
            except Exception:
                logger.exception("manual mill fill failed for idea %s", idea_id)
                verdict = {"executed": False, "skip_reason": "error"}
            note = trade_ideas_bridge.format_manual_fill_reply(verdict, idea_id)
            if note:
                await context.bot.send_message(chat_id, note[:4096])
        return

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

    if data == telegram_ui.CB_FEED:
        url = user_books.feed_url(user_id)
        if url:
            text = (
                "Idea feed — every mill card, same stream for everyone.\n\n"
                f"Open the feed: {url}\n"
                "Accept / Reject on the page writes to your paper book. "
                "(Link expires in about an hour; tap Idea feed again for a fresh one.)"
            )
        else:
            text = (
                "Idea feed needs DASHBOARD_PUBLIC_URL set on the server.\n"
                "Telegram Accept / Reject on idea cards still works."
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

    if data.startswith(telegram_ui.CB_TRADE_MORE_PREFIX):
        offer_id = data[len(telegram_ui.CB_TRADE_MORE_PREFIX) :]
        offer = user_books.get_offer(offer_id)
        if offer is None:
            await context.bot.send_message(
                chat_id,
                "Could not find that trade offer.",
                reply_markup=telegram_ui.main_keyboard(),
            )
            return
        try:
            await notify.send_offer_details_to_chat(context.bot, chat_id, offer)
        except Exception:
            logger.exception("See more failed for offer %s", offer_id)
            await context.bot.send_message(
                chat_id,
                "Could not load trade details right now.",
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
        "/start — welcome + menu (Open account, My Metrics, My book, Idea feed, Journal, Research)\n"
        "/status — current suggestion + paper PnL\n"
        "/performance — volume idea book (realized + unrealized)\n"
        "/me — your accepted-idea portfolio PnL (Close buttons on open trades)\n"
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


async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    await _handle_performance(update, context)


async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _reply(update, PAYWALL_MESSAGE)
        return

    await _handle_me(update, context)


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

    if _is_performance_query(user_text):
        await _handle_performance(update, context)
        return

    if _is_me_query(user_text):
        await _handle_me(update, context)
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


async def cmd_watchdog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle watchdog paper execution (admin/monitor only)."""
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not _is_macro_admin(user.id):
        await _reply(update, "Watchdog control is restricted to the monitor/admin account.")
        return

    args = [a.lower() for a in (context.args or [])]
    current = bot_config.watchdog_execute_enabled()
    if not args or args[0] in {"status", "?"} :
        await _reply(
            update,
            (
                f"Watchdog scan: {'on' if bot_config.WATCHDOG_ENABLED else 'off'}\n"
                f"Paper execute: {'on' if current else 'off'}\n"
                f"Allow shorts: {'yes' if bot_config.WATCHDOG_ALLOW_SHORTS else 'no'}\n\n"
                "Usage: /watchdog on | off | status"
            ),
        )
        return

    if args[0] in {"on", "enable", "1", "true"}:
        bot_config.set_watchdog_execute_enabled(True)
        await _reply(
            update,
            "Watchdog paper execution ON. "
            f"Shorts still {'allowed' if bot_config.WATCHDOG_ALLOW_SHORTS else 'shadow-only'}.",
        )
        return
    if args[0] in {"off", "disable", "0", "false"}:
        bot_config.set_watchdog_execute_enabled(False)
        await _reply(update, "Watchdog paper execution OFF — scan/shadow only.")
        return

    await _reply(update, "Usage: /watchdog on | off | status")


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
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("ideas", cmd_performance))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("macro", cmd_macro))
    app.add_handler(CommandHandler("watchdog", cmd_watchdog))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
