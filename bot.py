"""Telegram bot handlers — access gate, status, chat Q&A, and research."""

from __future__ import annotations

import asyncio
import logging
import re

from telegram import Update
from telegram.error import BadRequest
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
import pool
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


async def _reply(update: Update, text: str, *, markdown: bool = False,
                 **kwargs) -> None:
    """Reply, optionally rendering the copy's markdown.

    The fallback is the point. Telegram refuses an entire message if it cannot
    parse the entities, and a stray underscore or bracket in a handle is
    enough. On the withdrawal path that failure mode is severe: the money is
    already debited by the time the confirmation is sent, so a refused message
    means funds held with the tester told nothing. Unformatted text is a far
    better outcome than silence.
    """
    if update.message is None:
        return
    if markdown:
        try:
            await update.message.reply_text(
                text, parse_mode="Markdown", **kwargs
            )
            return
        except BadRequest:
            logger.warning("markdown parse failed; sending plain", exc_info=True)
    await update.message.reply_text(text, **kwargs)


async def _send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str,
                *, markdown: bool = False, **kwargs) -> None:
    """Push a message to a chat, degrading to plain text rather than failing."""
    if markdown:
        try:
            await context.bot.send_message(
                chat_id, text, parse_mode="Markdown", **kwargs
            )
            return
        except BadRequest:
            logger.warning("markdown parse failed; sending plain", exc_info=True)
    await context.bot.send_message(chat_id, text, **kwargs)


async def _notify_admins_new_user(context: ContextTypes.DEFAULT_TYPE, user) -> None:
    """Ping every pool admin with an Admit/Deny card for a new requester."""
    name = f"@{user.username}" if user.username else (user.full_name or "unknown")
    text = f"New user wants in: {name} (id {user.id})"
    for admin_id in pool.admin_ids():
        try:
            await context.bot.send_message(
                admin_id,
                text,
                reply_markup=telegram_ui.pool_admin_access_keyboard(user.id),
            )
        except Exception:
            logger.exception("Admin access ping failed for %s", admin_id)


async def _handle_gated_user(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """What a not-yet-allowed user sees. With the pool on, first contact files
    an access request and pings the admins; afterwards they see 'pending'."""
    user = update.effective_user
    if user is None:
        return
    if not bot_config.POOL_ENABLED:
        await _reply(update, PAYWALL_MESSAGE)
        return
    status = pool.request_access(user.id, _username(update))
    if status == "denied":
        await _reply(update, telegram_ui.DENIED_MESSAGE)
        return
    if status == "new":
        await _notify_admins_new_user(context, user)
    await _reply(update, telegram_ui.PENDING_APPROVAL_MESSAGE)


def _pool_intent_reply(result: dict, *, risk_label: str = "risk") -> str:
    """User-facing text for a pool.record_intent verdict."""
    if result.get("ok"):
        risk = float(result["risk_usd"])
        pct = float(bot_config.POOL_RISK_PCT) * 100
        return (
            f"You're in if it fills.\n\n"
            f"Reserved: ${risk:,.2f} at risk ({pct:.1f}% of your available cash). "
            "That is the most this trade can cost you if stopped out — not your "
            "full balance. Same fill price as the house; exits are automatic.\n\n"
            "I'll DM you either way — when it fills, or when it's pulled and "
            "your reserve comes back. A card can rest a couple of hours while "
            "the setup is still in play, so no news isn't bad news. "
            "/portfolio any time."
        )
    reason = result.get("reason")
    if reason == "already_recorded":
        return "Already recorded — you're on this order."
    if reason == "frozen":
        return (
            "New trade joins are paused while we verify the books. Your balance "
            "is safe; try again later."
        )
    if reason == "below_min_equity":
        return (
            f"Pool trades need at least ${float(result.get('minimum_usd') or 0):,.0f} "
            "cash. /deposit to top up."
        )
    if reason in ("not_funded", "no_available_cash"):
        return "No available cash for this one — /portfolio shows what's reserved."
    return f"Could not join ({reason})."


def _pool_hq_accept(offer_id: str, user_id: int) -> str:
    """A funded tester's Accept on an HQ card → pool intent (sync, executor)."""
    import live_pending

    offer = user_books.get_offer(offer_id)
    if offer is None:
        return "Could not find that trade offer."
    product_id = str(offer.get("product_id") or "")
    waiting = live_pending.get_pending(product_id)
    row = next(
        (r for r in waiting if str(r.get("cycle_id") or "") == str(offer_id)), None
    )
    if row is None:
        return (
            "This order has already gone on or been pulled — your Accept came "
            "after the window, so you're not in this trade. The next card is "
            "never far."
        )
    return _pool_intent_reply(pool.record_intent(str(offer_id), user_id))


DEMO_REF_PREFIX = "demo_"


def _pool_demo_accept(token: str, user_id: int) -> str:
    """Accept on a demo card. Real reservation, no order, self-clearing.

    Deliberately routed through `pool.record_intent` rather than faked: the
    reply a tester sees is then the real one, computed from their real
    available cash by the real sizing rule, so a demo cannot flatter the
    product by quoting a number the live path would not produce.

    Nothing can fill, and that is structural rather than careful. Every
    executor looks intents up **by ref** — a live pending cycle id or
    `mill_<id>` — and a `demo_` ref matches neither, so no code path exists
    that could turn this into a position. The watchdog's stale-intent sweep
    then sees a ref that is not active, releases the reserve, and sends the
    genuine "that order never fired, your money is back" DM within a minute.
    """
    return _pool_intent_reply(
        pool.record_intent(f"{DEMO_REF_PREFIX}{token}", user_id)
    )


def _pool_mill_accept(idea_id: int, user_id: int) -> str:
    """A funded tester's Accept on a mill card (sync, executor).

    Order matters: the intent is recorded **before** the fill is attempted, so
    the pooled aggregation in `execute_mill_idea` sees this tester's budget and
    they ride the same fill at the same price.

    If the fill is refused, the intent is released right here rather than left
    pending. Previously an Accept on an idea that could never fill reserved the
    money and said "you're in if it fills", and the tester heard nothing until
    the stale sweep caught it up to two hours later. The refusal reason is
    known at this moment; sitting on it helps nobody.
    """
    ref = f"mill_{idea_id}"
    if not trade_ideas_bridge.idea_pool_open(idea_id):
        return (
            "This idea has already filled or expired — your Accept came after "
            "the window, so you're not in this one."
        )

    recorded = pool.record_intent(ref, user_id)
    if not recorded.get("ok"):
        return _pool_intent_reply(recorded)

    if not trade_ideas_bridge.may_fill(user_id):
        return _pool_intent_reply(recorded)

    try:
        verdict = trade_ideas_bridge.request_manual_fill(idea_id, user_id)
    except Exception:
        logger.exception("pool-triggered mill fill failed for idea %s", idea_id)
        return _pool_intent_reply(recorded)

    if verdict.get("executed"):
        return _pool_fill_reply(recorded, verdict, user_id)

    # Refused, and we know why now.
    released = pool.release_intents(ref, status="missed")
    mine = next(
        (r for r in released if int(r["telegram_id"]) == int(user_id)), None
    )
    back = float(mine["risk_usd"]) if mine else float(recorded.get("risk_usd") or 0)
    why = trade_ideas_bridge.explain_skip(verdict.get("skip_reason"))
    return (
        f"Not filled — nothing was risked and your ${back:,.2f} is free "
        f"again.\n\n{why}\n\nI'll send the next card when one sets up."
    )


def _pool_fill_reply(recorded: dict, verdict: dict, user_id: int) -> str:
    """Confirm a tester is in, quoting the share they actually got.

    Their real share can be smaller than the reservation implied: the venue
    fills whole contracts, so a budget that did not grow the order still buys a
    pro-rata slice of it. Quote the stake, not the intent.
    """
    result = verdict.get("result") or {}
    trade_id = result.get("trade_id")
    fill = float(result.get("fill") or 0)
    stake = None
    if trade_id is not None:
        stake = next(
            (s for s in pool.open_stakes_for(int(trade_id))
             if int(s["telegram_id"]) == int(user_id)), None
        )
    if stake is None:
        return _pool_intent_reply(recorded)

    notional = float(stake["qty"]) * fill if fill else float(stake["cost_usd"])
    return (
        f"You're in — filled at ${fill:,.2f}.\n\n"
        f"Your share: ${notional:,.2f} notional, "
        f"${float(stake['risk_usd']):,.2f} at risk. Same fill price as the "
        "house. Exits are automatic — I'll DM you when it closes. "
        "/portfolio any time."
    )


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
        await _handle_gated_user(update, context)
        return

    if bot_config.POOL_ENABLED and pool.is_approved(user.id):
        p = pool.portfolio(user.id)
        lines = [telegram_ui.POOL_WELCOME_MESSAGE]
        if p.get("ok") and float(p.get("cash_usd") or 0) > 0:
            lines.append("")
            lines.append(telegram_ui.format_portfolio(p))
        await _reply(
            update,
            "\n".join(lines)[:4096],
            markdown=True,
            reply_markup=telegram_ui.pool_account_keyboard(),
        )
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
    user_id = query.from_user.id
    access.register_user(user_id, query.from_user.username)
    if not access.is_allowed(user_id):
        # Never edit the message: in the forum group the card is shared, and
        # editing it would blank the trade for everyone else.
        try:
            await query.answer(
                text="Access is invite-only — message the bot directly to request it.",
                show_alert=True,
            )
        except Exception:
            logger.debug("Gate answer failed", exc_info=True)
        return
    await query.answer()

    data = query.data or ""
    chat_id = query.message.chat_id if query.message else user_id

    # --- Tester pool: admin Admit/Deny, deposit decisions, Account buttons ---
    if data.startswith(telegram_ui.CB_POOL_DEMO_PREFIX):
        rest = data[len(telegram_ui.CB_POOL_DEMO_PREFIX):]
        choice, _, token = rest.partition(":")
        if choice not in ("yes", "no") or not token:
            return
        if choice == "no":
            reply = ("Skipped — nothing reserved. That is all a Reject does: "
                     "no position, no cost.")
        elif not pool.is_funded(user_id):
            reply = (
                "Your account has no funds yet, so this Accept was not "
                "placed. /deposit to join live trades."
            )
        else:
            loop = asyncio.get_running_loop()
            try:
                reply = await loop.run_in_executor(
                    None, _pool_demo_accept, token, user_id
                )
            except Exception:
                logger.exception("pool demo accept failed for %s", token)
                reply = "Could not record your Accept — try again."
        try:
            await context.bot.send_message(user_id, reply)
        except Exception:
            logger.exception("Pool demo reply DM failed for %s", user_id)
        return

    if data.startswith(telegram_ui.CB_POOL_USER_PREFIX):
        if not pool.is_admin(user_id):
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in ("approve", "deny"):
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            return
        if parts[1] == "approve":
            pool.approve_user(target_id, admin_id=user_id)
            invite_line = ""
            if config.POOL_FORUM_CHAT_ID:
                try:
                    link = await context.bot.create_chat_invite_link(
                        chat_id=config.POOL_FORUM_CHAT_ID, member_limit=1
                    )
                    invite_line = (
                        "\n\nTrade cards and research live in the group — "
                        f"join here (one-time link): {link.invite_link}"
                    )
                except Exception:
                    logger.exception("Forum invite link failed for %s", target_id)
            try:
                await _send(
                    context, target_id,
                    (telegram_ui.POOL_WELCOME_MESSAGE + invite_line)[:4096],
                    markdown=True,
                    reply_markup=telegram_ui.pool_account_keyboard(),
                )
            except Exception:
                logger.exception("Welcome DM failed for %s", target_id)
            await context.bot.send_message(
                chat_id, f"Admitted {target_id}. They got the welcome + invite."
            )
        else:
            pool.deny_user(target_id, admin_id=user_id)
            try:
                await context.bot.send_message(target_id, telegram_ui.DENIED_MESSAGE)
            except Exception:
                logger.debug("Deny DM failed for %s", target_id, exc_info=True)
            await context.bot.send_message(chat_id, f"Denied {target_id}.")
        return

    if data.startswith(telegram_ui.CB_POOL_WITHDRAW_PREFIX):
        if not pool.is_admin(user_id):
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in ("approve", "reject"):
            return
        try:
            wid = int(parts[2])
        except ValueError:
            return

        approve = parts[1] == "approve"
        result = pool.decide_withdrawal(wid, admin_id=user_id, approve=approve)
        if not result.get("ok"):
            await context.bot.send_message(
                chat_id, f"Withdrawal #{wid}: {result.get('reason')}"
            )
            return

        target_id = int(result["telegram_id"])
        amount = float(result["amount_usd"])
        if approve:
            # The watchdog does the sending; approving only queues it. Keeping
            # the network call out of the callback means a Telegram retry
            # cannot become a second payment.
            await context.bot.send_message(
                chat_id,
                f"Withdrawal #{wid} approved — ${amount:,.2f} will go out on "
                "the next sweep (within a minute).",
            )
            try:
                await context.bot.send_message(
                    target_id,
                    f"Withdrawal #{wid} approved — ${amount:,.2f} is being "
                    "sent now. You'll get a confirmation here shortly.",
                )
            except Exception:
                logger.exception("Withdrawal approval DM failed for %s", target_id)
        else:
            await context.bot.send_message(
                chat_id, f"Withdrawal #{wid} rejected and refunded in full."
            )
            try:
                await context.bot.send_message(
                    target_id,
                    f"Withdrawal #{wid} wasn't sent, and ${amount:,.2f} has "
                    "been returned to your balance in full.\n\n"
                    "Ask an admin if you're not sure why.",
                )
            except Exception:
                logger.exception("Withdrawal rejection DM failed for %s", target_id)
        return

    if data.startswith(telegram_ui.CB_POOL_WALLET_PREFIX):
        if not pool.is_admin(user_id):
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in ("approve", "reject"):
            return
        try:
            row_id = int(parts[2])
        except ValueError:
            return
        result = pool.decide_wallet_change(
            row_id, admin_id=user_id, approve=parts[1] == "approve"
        )
        if not result.get("ok"):
            await context.bot.send_message(
                chat_id, f"Wallet change #{row_id}: {result.get('reason')}"
            )
            return
        target_id = int(result["telegram_id"])
        address = str(result["address"])
        if result.get("approved"):
            held = str(result.get("payouts_blocked_until") or "")
            await context.bot.send_message(
                chat_id,
                f"Payout address for {target_id} is now {address}. "
                f"Withdrawals held until {held}.",
            )
            try:
                await context.bot.send_message(
                    target_id,
                    "Your payout address was updated to:\n"
                    f"`{address}`\n\n"
                    f"Withdrawals are held until {held}. If this wasn't you, "
                    "reply now — that hold is there so this can still be "
                    "undone.",
                )
            except Exception:
                logger.exception("Wallet-change DM failed for %s", target_id)
        else:
            await context.bot.send_message(
                chat_id, f"Rejected the wallet change for {target_id}."
            )
            try:
                await context.bot.send_message(
                    target_id,
                    "Your address change wasn't approved. Your payout wallet "
                    "is unchanged — /wallet shows it. Message the admin if "
                    "you need it moved.",
                )
            except Exception:
                logger.exception("Wallet-reject DM failed for %s", target_id)
        return

    if data.startswith(telegram_ui.CB_POOL_DEPOSIT_PREFIX):
        if not pool.is_admin(user_id):
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in ("credit", "deny"):
            return
        try:
            request_id = int(parts[2])
        except ValueError:
            return
        result = pool.decide_deposit(
            request_id, admin_id=user_id, approve=parts[1] == "credit"
        )
        if not result.get("ok"):
            await context.bot.send_message(
                chat_id,
                f"Deposit request #{request_id}: {result.get('reason')}"
                + (f" ({result.get('status')})" if result.get("status") else ""),
            )
            return
        target_id = int(result["telegram_id"])
        amount = float(result["amount_usd"])
        if result.get("status") == "credited":
            await context.bot.send_message(
                chat_id,
                f"Credited ${amount:,.2f} to {target_id} "
                f"(cash now ${float(result.get('cash_usd') or 0):,.2f}).",
            )
            try:
                await context.bot.send_message(
                    target_id,
                    f"Deposit credited: ${amount:,.2f}.\n"
                    f"Cash balance: ${float(result.get('cash_usd') or 0):,.2f}.\n\n"
                    f"Each Accept still only risks about "
                    f"{bot_config.POOL_RISK_PCT * 100:.1f}% of your available cash "
                    f"(e.g. ${float(result.get('cash_usd') or 0) * bot_config.POOL_RISK_PCT:,.2f} "
                    f"on a full-balance Accept right now) — sizes stay small while "
                    "we solidify the strategy.\n"
                    "Trade cards will show your size. /portfolio any time.",
                )
            except Exception:
                logger.exception("Credit DM failed for %s", target_id)
        else:
            await context.bot.send_message(
                chat_id, f"Denied deposit request #{request_id} ({target_id})."
            )
            try:
                await context.bot.send_message(
                    target_id,
                    f"Your deposit request for ${amount:,.2f} was not credited. "
                    "If you already sent funds, message the admin.",
                )
            except Exception:
                logger.debug("Deposit deny DM failed", exc_info=True)
        return

    if data == telegram_ui.CB_POOL_PORTFOLIO:
        loop = asyncio.get_running_loop()

        def _load_portfolio() -> str:
            spots = research.get_spot_prices()
            return telegram_ui.format_portfolio(pool.portfolio(user_id, spots))

        try:
            text = await loop.run_in_executor(None, _load_portfolio)
        except Exception:
            logger.exception("Portfolio load failed for %s", user_id)
            text = "Could not load your portfolio right now."
        await context.bot.send_message(
            user_id, text[:4096], reply_markup=telegram_ui.pool_account_keyboard()
        )
        return

    if data == telegram_ui.CB_POOL_DEPOSIT:
        await context.bot.send_message(
            user_id, telegram_ui.format_deposit_instructions()
        )
        return

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

        # Pool testers: Accept means a real claim on the shared fill. Their
        # reply is a DM — personal money never lands in the group topic —
        # and an unfunded tester is pointed at /deposit instead of falling
        # into the demo path (whose reply would post into the group).
        if (
            bot_config.POOL_ENABLED
            and pool.is_approved(user_id)
            and not trade_ideas_bridge.is_fill_operator(user_id)
        ):
            loop = asyncio.get_running_loop()
            if not pool.is_funded(user_id):
                reply = (
                    "Your account has no funds yet, so this Accept was not "
                    "placed. /deposit in our private chat to join live trades."
                )
            elif decision == "accept" and status in ("recorded", "duplicate"):
                try:
                    reply = await loop.run_in_executor(
                        None, _pool_mill_accept, idea_id, user_id
                    )
                except Exception:
                    logger.exception("pool mill accept failed for idea %s", idea_id)
                    reply = "Could not record your Accept — try again."
            elif decision == "reject":
                reply = "Noted — you're staying out of this one."
            else:
                reply = trade_ideas_bridge.format_decision_reply(
                    status, decision, idea_id
                )
            try:
                await context.bot.send_message(user_id, reply)
            except Exception:
                logger.exception("Pool mill reply DM failed for %s", user_id)
            return

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

        # Pool testers: Accept joins the real shared order. DM only — the
        # demo path below would post their reply into the group topic.
        if bot_config.POOL_ENABLED and pool.is_approved(user_id):
            if not pool.is_funded(user_id):
                reply = (
                    "Your account has no funds yet, so this Accept was not "
                    "placed. /deposit in our private chat to join live trades."
                )
            else:
                loop = asyncio.get_running_loop()
                try:
                    reply = await loop.run_in_executor(
                        None, _pool_hq_accept, offer_id, user_id
                    )
                except Exception:
                    logger.exception("pool HQ accept failed for offer %s", offer_id)
                    reply = "Could not record your Accept — try again."
            try:
                await context.bot.send_message(user_id, reply)
            except Exception:
                logger.exception("Pool HQ reply DM failed for %s", user_id)
            return

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

        if bot_config.POOL_ENABLED and pool.is_approved(user_id):
            try:
                await context.bot.send_message(
                    user_id, "Noted — you're staying out of this one."
                )
            except Exception:
                logger.debug("Pool reject DM failed", exc_info=True)
            return

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
        await _handle_gated_user(update, context)
        return

    if bot_config.POOL_ENABLED and pool.is_approved(user.id):
        await update.message.reply_text(
            "Commands:\n"
            "/portfolio — your cash, positions, and P&L\n"
            "/deposit — fund your account (sizes stay small while we prove the strategy)\n"
            "/withdraw — take money out, back to your registered wallet\n"
            "/wallet — the address you fund from and are paid back to\n"
            "/start — welcome + how risk works\n"
            "/help — this message\n\n"
            "Trade cards arrive here as private messages. Accept joins about "
            f"{bot_config.POOL_RISK_PCT * 100:.1f}% of your available cash at risk "
            "on that trade — not your full balance.",
            reply_markup=telegram_ui.pool_account_keyboard(),
        )
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
        await _handle_gated_user(update, context)
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
        await _handle_gated_user(update, context)
        return

    await _handle_performance(update, context)


async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _handle_gated_user(update, context)
        return

    # Pool users: /me means their real money now.
    if bot_config.POOL_ENABLED and pool.is_approved(user.id):
        await cmd_portfolio(update, context)
        return

    await _handle_me(update, context)


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The tester's real book: cash, open stakes MTM, realized, deposits."""
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _handle_gated_user(update, context)
        return

    if not bot_config.POOL_ENABLED:
        await _reply(update, "The live pool is not open yet.")
        return

    await update.message.chat.send_action("typing")
    loop = asyncio.get_running_loop()

    def _load() -> str:
        spots = research.get_spot_prices()
        return telegram_ui.format_portfolio(pool.portfolio(user.id, spots))

    try:
        text = await loop.run_in_executor(None, _load)
    except Exception:
        logger.exception("Portfolio failed for %s", user.id)
        await _reply(update, "Could not load your portfolio right now.")
        return
    await update.message.reply_text(
        text[:4096], reply_markup=telegram_ui.pool_account_keyboard()
    )


async def cmd_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/deposit` shows instructions; `/deposit <amount> [txid]` files a request."""
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _handle_gated_user(update, context)
        return

    if not bot_config.POOL_ENABLED or not pool.is_approved(user.id):
        await _reply(update, "The live pool is not open for your account yet.")
        return

    wallet = pool.get_wallet(user.id)
    args = context.args or []
    if not args:
        await _reply(
            update,
            telegram_ui.format_deposit_instructions(
                wallet=str(wallet["address"]) if wallet else None
            ),
            markdown=True,
        )
        return

    try:
        amount = float(str(args[0]).replace("$", "").replace(",", ""))
    except ValueError:
        await _reply(update, "Usage: /deposit 1000  (optionally: /deposit 1000 <txid>)")
        return
    txid = str(args[1]) if len(args) > 1 else None

    result = pool.request_deposit(user.id, amount, txid=txid)
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "below_minimum":
            await _reply(
                update,
                f"Minimum deposit is ${float(result.get('minimum_usd') or 0):,.0f}.",
            )
        elif reason == "already_pending":
            await _reply(
                update,
                "You already have a deposit request pending review — "
                "you'll get a message when it's credited.",
            )
        elif reason == "wallet_required":
            await _reply(
                update,
                "Register the wallet you're sending from first:\n\n"
                "/wallet 0x<your address>\n\n"
                "It's how we match your transfer when it arrives, and it's "
                "the only address we send withdrawals back to.",
            )
        elif reason in ("txid_required", "txid_malformed"):
            await _reply(
                update,
                "Send the transaction hash with the amount so we can match "
                "your transfer on-chain:\n\n"
                f"/deposit {amount:,.0f} 0x<transaction hash>\n\n"
                "Your wallet shows it as the transaction ID after the "
                "transfer confirms. Without it we cannot tell your deposit "
                "apart from other funds arriving at the same address.",
            )
        elif reason == "txid_already_claimed":
            await _reply(
                update,
                "That transaction hash is already on deposit request "
                f"#{result.get('request_id')}. If you sent a second transfer, "
                "use that transfer's own hash.",
            )
        else:
            await _reply(update, f"Could not file the request ({reason}).")
        return

    request_id = int(result["request_id"])
    await _reply(
        update,
        f"Got it — watching the exchange for that transfer (#{request_id}).\n\n"
        "You'll be credited automatically the moment it settles, and I'll "
        "message you here with your balance. Nothing else for you to do.",
    )
    name = f"@{user.username}" if user.username else str(user.id)
    txid_line = f"\ntxid: `{result['txid']}`" if result.get("txid") else ""
    wallet_line = (
        f"\nregistered wallet: `{wallet['address']}`" if wallet else ""
    )
    # Deposits now land at the venue directly, so there is no sweep to wait on
    # and the reconciler counts the funds on arrival. What is left to check is
    # attribution: that the amount matches, and that it came from the address
    # this tester registered.
    inbound = pool.pending_inbound_usd()
    inbound_line = (
        f"\nUncredited tester claims on venue cash: ${inbound:,.2f}"
        if inbound > amount
        else ""
    )
    for admin_id in pool.admin_ids():
        try:
            await context.bot.send_message(
                admin_id,
                f"Deposit request #{request_id}: {name} (id {user.id}) says they "
                f"sent ${amount:,.2f}.{txid_line}{wallet_line}{inbound_line}\n\n"
                "FYI only — the watcher credits this automatically the moment "
                "the transfer settles on Coinbase, and tells them. Credit below "
                "only if you want it booked before it has arrived, which gives "
                "them a claim the venue cannot yet cover.",
                reply_markup=telegram_ui.pool_admin_deposit_keyboard(request_id),
            )
        except Exception:
            logger.exception("Deposit admin ping failed for %s", admin_id)


async def cmd_democard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/democard [id] [btc|eth] [long|short]` — send one demo trade card.

    Admin only, and from Telegram rather than a server script so it can be
    fired mid-recording without an SSH session in the shot.
    """
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not bot_config.POOL_ENABLED or not pool.is_admin(user.id):
        return

    import demo_card

    opts = demo_card.parse_args(context.args or [], default_id=user.id)
    kwargs = {
        "product": str(opts["product"]), "side": str(opts["side"]),
        "mirror": bool(opts["mirror"]), "source": opts["source"],
        "trade_id": opts["trade_id"],
    }
    loop = asyncio.get_running_loop()

    if opts["everyone"]:
        batch = await loop.run_in_executor(
            None, lambda: demo_card.send_many(demo_card.recipients(), **kwargs)
        )
        sent, failed = batch["sent"], batch["failed"]
        if not sent:
            reasons = ", ".join(sorted({str(f.get("reason")) for f in failed}))
            await _reply(update, f"No cards sent ({reasons or 'no recipients'}).")
            return
        quoted = sum(1 for s in sent if s.get("quotes_a_size"))
        lines = [
            f"Demo card sent to {len(sent)} account(s) — "
            f"{sent[0]['product']} "
            f"{'long' if sent[0]['side'] == 'buy' else 'short'}"
            + (f" (mirrors live {sent[0]['mirrored_source']} "
               f"#{sent[0]['mirrored_trade_id']})"
               if sent[0].get("mirrored_trade_id") else ""),
            f"{quoted} of them are funded and saw a real size; "
            f"{len(sent) - quoted} were invited to /deposit.",
        ]
        if failed:
            lines.append(
                "Could not reach: "
                + ", ".join(f"{f['telegram_id']} ({f.get('reason')})"
                            for f in failed)
            )
        await _reply(update, "\n".join(lines))
        return

    result = await loop.run_in_executor(
        None, lambda: demo_card.send(int(opts["telegram_id"]), **kwargs)
    )

    if not result.get("ok"):
        reasons = {
            "not_approved": "that id is not approved — Admit them first, or "
                            "the card will say 'Access required' instead of "
                            "showing a size",
            "no_spot": "could not read a spot price just now",
            "no_open_trade": "nothing is open to mirror — drop the 'live' "
                             "argument for a synthetic setup off current spot",
            "send_failed": "could not DM that id — they must have messaged "
                           "the bot at least once",
            "pool_disabled": "POOL_ENABLED is off",
            "render_failed": "card render failed — check the logs",
        }
        await _reply(
            update,
            f"No card sent: {reasons.get(str(result.get('reason')), result.get('reason'))}",
        )
        return

    origin = (
        f"mirrors live {result['mirrored_source']} #{result['mirrored_trade_id']}"
        if result.get("mirrored_trade_id") else "synthetic setup off spot"
    )
    lines = [
        f"Demo card sent to {opts['telegram_id']} — "
        f"{result['product']} {'long' if result['side'] == 'buy' else 'short'} "
        f"({origin})",
        f"Entry ${result['entry']:,.2f} · stop ${result['stop_loss']:,.2f} "
        f"(spot ${result['spot']:,.2f})",
    ]
    drift = result.get("drift_pct")
    if drift is not None and drift >= 0.5:
        # Worth knowing before filming: a mirrored entry can be well behind
        # the market, which looks odd on camera even though it is real.
        lines.append(
            f"Heads up: spot is {drift:.1f}% off that entry, since the real "
            f"trade opened earlier."
        )
    if result["quotes_a_size"]:
        lines.append(
            f"Their Accept would risk ${result['risk_usd']:,.2f} on "
            f"${result['notional_usd']:,.0f} notional."
        )
    else:
        # Say it here rather than let it be discovered on playback.
        lines.append(
            "That account is unfunded, so the card invites them to /deposit "
            "instead of quoting a size."
        )
    lines.append(
        "Accept reserves their real budget; nothing can fill, and the reserve "
        "comes back within a minute with the real 'never fired' message."
    )
    await _reply(update, "\n".join(lines))


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/wallet` shows the payout address; `/wallet 0x…` sets or changes it."""
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))
    if not access.is_allowed(user.id):
        await _handle_gated_user(update, context)
        return
    if not bot_config.POOL_ENABLED or not pool.is_approved(user.id):
        await _reply(update, "The live pool is not open for your account yet.")
        return

    args = context.args or []
    if not args:
        await _reply(
            update,
            telegram_ui.format_wallet_status(
                pool.get_wallet(user.id),
                change=pool.get_wallet_change_request(user.id),
            ),
            markdown=True,
        )
        return

    existing = pool.get_wallet(user.id)
    given = str(args[0])
    result = (
        pool.request_wallet_change(user.id, given)
        if existing is not None
        else pool.register_wallet(user.id, given)
    )

    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "malformed":
            await _reply(
                update,
                "That doesn't look like an Ethereum address. It should be "
                "`0x` followed by 40 characters — copy it from your wallet "
                "rather than typing it.",
            )
        elif reason == "same_address":
            await _reply(update, "That's already your registered wallet.")
        elif reason == "already_pending":
            await _reply(
                update,
                "You already have an address change waiting on admin review.",
            )
        elif reason == "address_taken":
            # Deliberately not saying whose: whether an address is in the book
            # is not this user's information.
            await _reply(
                update,
                "That address can't be registered. If it's yours, message the "
                "admin.",
            )
        else:
            await _reply(update, f"Could not register that ({reason}).")
        return

    if result.get("unchanged"):
        await _reply(update, "That's already your registered wallet.")
        return

    # First registration: live immediately, because an account with no money
    # has nothing to redirect.
    if "request_id" not in result:
        await _reply(
            update,
            "Registered:\n"
            f"`{result['address']}`\n\n"
            "Send your deposit from this wallet — that's what confirms it's "
            "yours, and withdrawals return here and nowhere else. Check it "
            "carefully; /wallet shows it any time.\n\n"
            "Next: /deposit",
        )
        return

    # A change: admin review, and the tester hears about it on the old address
    # too, since they are the one person who would know it was not them.
    hours = float(result.get("cooldown_hours") or 0)
    await _reply(
        update,
        f"Change requested to:\n`{result['address']}`\n\n"
        f"An admin reviews it first, and withdrawals are held for {hours:.0f}h "
        "afterwards. If you didn't request this, say so now — that delay "
        "exists for exactly this reason.",
    )
    name = f"@{user.username}" if user.username else str(user.id)
    for admin_id in pool.admin_ids():
        try:
            await context.bot.send_message(
                admin_id,
                f"Payout address change: {name} (id {user.id})\n\n"
                f"from `{result.get('previous')}`\n"
                f"to   `{result['address']}`\n\n"
                "Confirm out-of-band that this is really them before approving "
                "— re-pointing the payout address is what an account takeover "
                f"would do first. Approval holds their withdrawals {hours:.0f}h.",
                reply_markup=telegram_ui.pool_admin_wallet_keyboard(
                    int(result["request_id"])
                ),
            )
        except Exception:
            logger.exception("Wallet-change admin ping failed for %s", admin_id)


async def cmd_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/withdraw [amount] — take money out, back to the registered wallet."""
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not bot_config.POOL_ENABLED or not pool.is_approved(user.id):
        await _handle_gated_user(update, context)
        return

    account = pool.get_account(user.id)
    if account is None:
        await _reply(update, "No account yet — /deposit to get started.")
        return

    available = pool.withdrawable_usd(user.id)
    maximum = pool.max_withdrawal_usd(user.id)
    reserved = float(account["reserved_usd"])
    args = context.args or []

    if not args:
        lines = [
            "*Withdraw*",
            "",
            f"Available now: *${available:,.2f}*",
        ]
        if reserved > 0:
            lines.append(
                f"In open trades: ${reserved:,.2f} — free once those close."
            )
        lines += [
            f"Most you can take: *${maximum:,.2f}*",
            "",
            f"Minimum ${float(bot_config.POOL_MIN_WITHDRAWAL_USD):,.0f}. The "
            "network fee comes out of your balance on top of the amount, so "
            "you receive exactly what you ask for.",
            "",
            "Funds go back to the wallet you registered, and nowhere else "
            "(/wallet to check).",
            "",
            f"`/withdraw 100`  or  `/withdraw {maximum:.2f}` for the maximum",
        ]
        await _reply(update, "\n".join(lines), markdown=True)
        return

    try:
        amount = round(float(str(args[0]).lstrip("$").replace(",", "")), 2)
    except ValueError:
        await _reply(update, "Usage: /withdraw 100")
        return

    result = pool.request_withdrawal(user.id, amount)
    if not result.get("ok"):
        await _reply(update, _withdrawal_refusal(result, maximum))
        return

    wid = int(result["withdrawal_id"])
    await _reply(
        update,
        f"Withdrawal #{wid} queued: *${amount:,.2f}*\n"
        f"To: `{result['address']}`\n\n"
        f"${float(result['debited_usd']):,.2f} is held from your balance "
        "(the extra covers the network fee; anything unused comes back).\n\n"
        "You'll get a message here the moment it's sent.",
        markdown=True,
    )

    for admin in pool.admin_ids():
        try:
            await context.bot.send_message(
                admin,
                f"Withdrawal #{wid}: *${amount:,.2f}* for "
                f"`{user.id}` ({user.username or 'no handle'})\n"
                f"To: `{result['address']}`\n"
                f"Balance after hold: ${float(result['cash_usd']):,.2f}",
                parse_mode="Markdown",
                reply_markup=telegram_ui.pool_admin_withdrawal_keyboard(wid),
            )
        except Exception:
            logger.exception("Withdrawal admin card failed for %s", admin)


def _withdrawal_refusal(result: dict, maximum: float) -> str:
    """Say why in terms the tester can act on, not the internal reason code."""
    reason = result.get("reason")
    if reason == "below_minimum":
        return (f"Minimum withdrawal is ${float(result['minimum_usd']):,.0f} — "
                "the network fee is flat, so smaller amounts lose too much to it.")
    if reason == "insufficient_available":
        return (
            f"You have ${float(result['available_usd']):,.2f} available, and the "
            f"most you can withdraw is ${float(result.get('max_usd') or 0):,.2f} "
            "once the network fee is covered.\n\n"
            "Money committed to open trades frees up when they close."
        )
    if reason in ("unverified", "no_wallet", "wallet_required"):
        return (
            "Your payout wallet isn't confirmed yet. We only send funds back to "
            "a wallet we've seen a deposit arrive from — that's what stops "
            "anyone who got into your Telegram redirecting your money.\n\n"
            "/wallet to check."
        )
    if reason == "wallet_cooldown":
        return ("Your payout address changed recently, so payouts are on hold "
                "briefly. This protects you if the change wasn't yours.")
    if reason == "user_daily_cap":
        return (f"That would pass the daily limit "
                f"(${float(result['cap_usd']):,.0f}). "
                f"${float(result['already_usd']):,.2f} already went out today.")
    if reason == "global_daily_cap":
        return "The pool's daily withdrawal limit is reached. Try tomorrow."
    if reason == "above_max":
        return (f"Single withdrawals are capped at "
                f"${float(result['maximum_usd']):,.0f}.")
    if reason in ("halted", "disabled"):
        return ("Withdrawals are paused while we check something. Your balance "
                "is untouched and an admin has been alerted.")
    return f"Couldn't queue that ({reason})."


async def cmd_payouts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /payouts [resume] — payout queue state and the halt switch."""
    user = update.effective_user
    if user is None or update.message is None or not pool.is_admin(user.id):
        return

    args = context.args or []
    if args and str(args[0]).lower() == "resume":
        pool.resume_payouts()
        await _reply(update, "Payouts resumed.")
        return
    if args and str(args[0]).lower() == "halt":
        pool.halt_payouts(f"halted by admin {user.id}")
        await _reply(update, "Payouts halted.")
        return

    halted = pool.payouts_halted()
    lines = [f"Payouts: {'HALTED — ' + halted if halted else 'running'}", ""]
    for status in ("requested", "approved", "submitting", "submitted", "unknown"):
        rows = pool.pending_withdrawals(status)
        if rows:
            lines.append(f"{status}: {len(rows)}")
            for r in rows[:5]:
                lines.append(
                    f"   #{r['id']} ${float(r['amount_usd']):,.2f} → {r['telegram_id']}"
                )
    lines.append("")
    lines.append(f"Out in last 24h: ${pool.withdrawn_since(None):,.2f}")
    lines.append("\n/payouts halt  |  /payouts resume")
    await _reply(update, "\n".join(lines))


async def cmd_assign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /assign <coinbase_tx_id> <telegram_id> — claim an orphan deposit.

    For a transfer that arrived without a usable hash, which the watcher
    records but deliberately refuses to apportion.
    """
    user = update.effective_user
    if user is None or update.message is None or not pool.is_admin(user.id):
        return

    args = context.args or []
    if len(args) < 2:
        pending = pool.unmatched_chain_deposits()
        if not pending:
            await _reply(update, "No unclaimed deposits.")
            return
        lines = ["Unclaimed deposits:\n"]
        for row in pending:
            lines.append(
                f"${float(row['amount_usd']):,.2f} — {row['cb_tx_id']}\n"
                f"   hash {row['txid']}  seen {row['first_seen_at']}"
            )
        lines.append("\n/assign <coinbase_tx_id> <telegram_id>")
        await _reply(update, "\n".join(lines))
        return

    try:
        target = int(args[1])
    except ValueError:
        await _reply(update, "Usage: /assign <coinbase_tx_id> <telegram_id>")
        return

    result = pool.assign_chain_deposit(str(args[0]), target, admin_id=user.id)
    if not result.get("ok"):
        await _reply(update, f"Could not assign it ({result.get('reason')}).")
        return

    amount = float(result["amount_usd"])
    await _reply(update, f"Assigned ${amount:,.2f} to {target}.")
    try:
        await context.bot.send_message(
            target,
            f"Deposit received: ${amount:,.2f} USDC.\n"
            f"Cash balance: ${float(result.get('cash_usd') or 0):,.2f}.\n\n"
            "You can Accept trade cards now — /portfolio any time.",
        )
    except Exception:
        logger.exception("Assign DM failed for %s", target)


async def cmd_credit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin escape hatch: /credit <telegram_id> <usd> [note]."""
    await _admin_cash_command(update, context, kind="credit")


async def cmd_debit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin escape hatch: /debit <telegram_id> <usd> [note]."""
    await _admin_cash_command(update, context, kind="debit")


async def _admin_cash_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, kind: str
) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not pool.is_admin(user.id):
        await _reply(update, f"/{kind} is restricted to pool admins.")
        return

    args = context.args or []
    if len(args) < 2:
        await _reply(update, f"Usage: /{kind} <telegram_id> <usd> [note]")
        return
    try:
        target_id = int(args[0])
        amount = float(str(args[1]).replace("$", "").replace(",", ""))
    except ValueError:
        await _reply(update, f"Usage: /{kind} <telegram_id> <usd> [note]")
        return
    note = " ".join(args[2:]) if len(args) > 2 else None

    if kind == "credit":
        result = pool.credit(target_id, amount, admin_id=user.id, note=note)
    else:
        result = pool.debit(target_id, amount, admin_id=user.id, note=note)

    if not result.get("ok"):
        detail = result.get("reason")
        if detail == "insufficient_available":
            detail = (
                f"insufficient available cash "
                f"(${float(result.get('available_usd') or 0):,.2f})"
            )
        await _reply(update, f"/{kind} failed: {detail}")
        return
    await _reply(
        update,
        f"{kind.title()}ed ${amount:,.2f} for {target_id} — "
        f"cash now ${float(result.get('cash_usd') or 0):,.2f}.",
    )
    verb = "credited to" if kind == "credit" else "debited from"
    try:
        await context.bot.send_message(
            target_id,
            f"${amount:,.2f} was {verb} your account"
            + (f" ({note})" if note else "")
            + ". /portfolio for your balance.",
        )
    except Exception:
        logger.debug("Cash command DM failed for %s", target_id, exc_info=True)


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _handle_gated_user(update, context)
        return

    await _handle_chart(update, context)


async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return

    access.register_user(user.id, _username(update))

    if not access.is_allowed(user.id):
        await _handle_gated_user(update, context)
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
        await _handle_gated_user(update, context)
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
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("deposit", cmd_deposit))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("democard", cmd_democard))
    app.add_handler(CommandHandler("assign", cmd_assign))
    app.add_handler(CommandHandler("withdraw", cmd_withdraw))
    app.add_handler(CommandHandler("payouts", cmd_payouts))
    app.add_handler(CommandHandler("credit", cmd_credit))
    app.add_handler(CommandHandler("debit", cmd_debit))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("macro", cmd_macro))
    app.add_handler(CommandHandler("watchdog", cmd_watchdog))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
