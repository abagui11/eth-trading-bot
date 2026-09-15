"""Telegram delivery: per-subscriber DMs with chart, rationale, and PnL footer."""

from __future__ import annotations

import asyncio
import html
import logging
from pathlib import Path

from telegram import Bot
from telegram.constants import ParseMode

import access
import bot_config
import config
import display_summary
import paper
import telegram_ui
import user_books
from critic import AuditVerdict, split_rationale
from models import Suggestion

logger = logging.getLogger(__name__)


def forum_trades_target() -> tuple[int, int | None] | None:
    """(chat_id, thread_id) for the forum Trades topic, or None for DM mode.

    The hybrid UX posts each trade card ONCE into the private forum group
    instead of DM-per-subscriber. Unset env falls back to DM broadcast so dev
    boxes without a forum keep working.
    """
    if not config.POOL_FORUM_CHAT_ID:
        return None
    return (config.POOL_FORUM_CHAT_ID, config.POOL_FORUM_TRADES_THREAD_ID)


def forum_research_target() -> tuple[int, int | None] | None:
    """(chat_id, thread_id) for the forum Research topic, or None for DM mode."""
    if not config.POOL_FORUM_CHAT_ID:
        return None
    return (config.POOL_FORUM_CHAT_ID, config.POOL_FORUM_RESEARCH_THREAD_ID)


def format_rationale_text(rationale: str) -> str:
    """Normalize paragraph breaks for Telegram readability."""
    text = rationale.strip()
    if not text:
        return ""
    # Collapse runs of whitespace/newlines into paragraph breaks.
    paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    if len(paragraphs) == 1 and len(paragraphs[0]) > 400:
        # Legacy wall-of-text: break before common section starters.
        import re

        single = paragraphs[0]
        breaks = (
            r"(?=\b(?:Multiple active|A H\d+|Price is currently|The 24h range|"
            r"Setup state|Two pending|On H\d|Monday Low|No R/R|Waiting for)\b)"
        )
        parts = [p.strip() for p in re.split(breaks, single) if p.strip()]
        if len(parts) > 1:
            paragraphs = parts
    return "\n\n".join(paragraphs)


def build_caption(
    suggestion: Suggestion,
    *,
    telegram_id: int | None = None,
    offer_id: str | None = None,
    display_summary_text: str | None = None,
    resting: bool | None = None,
    spot: float | None = None,
) -> str:
    """Short caption for the chart photo (Telegram limit: 1024 characters)."""
    return display_summary.build_card_body(
        suggestion,
        display_summary=display_summary_text,
        telegram_id=telegram_id,
        offer_id=offer_id,
        resting=resting,
        spot=spot,
    )


def build_caption_html(
    suggestion: Suggestion,
    *,
    telegram_id: int | None = None,
    offer_id: str | None = None,
    display_summary_text: str | None = None,
    resting: bool | None = None,
    spot: float | None = None,
) -> str:
    """The same caption with the execution verdict in bold.

    Everything except our own ``<b>`` comes from ``html.escape``, so prose the
    model wrote can never open a tag. Send the result *unsliced*: Telegram
    measures the 1024 limit after parsing entities, so the tags cost nothing,
    but cutting the string could split one in half and fail the whole send.
    ``build_card_body`` already bounds the visible text.
    """
    body = build_caption(
        suggestion,
        telegram_id=telegram_id,
        offer_id=offer_id,
        display_summary_text=display_summary_text,
        resting=resting,
        spot=spot,
    )
    marked = html.escape(body)
    note = display_summary.execution_banner(suggestion, spot=spot, resting=resting)
    if note is not None:
        headline = html.escape(note.headline)
        # Absent only if the caption was truncated past it; plain text then.
        marked = marked.replace(headline, f"<b>{headline}</b>", 1)
    return marked


# A resting HQ card promises "it executes only if BTC rises to $X". Each of
# these is the answer to that promise, and without them the card just goes
# quiet and keeps looking live on the subscriber's screen.
_PENDING_HEADLINES = {
    "filled": "Filled — this position is now open.",
    "refused": "Never filled — the order could not be placed.",
    "missed": "Never filled — the setup is gone.",
    "cancelled": "Never filled — Eva pulled the order.",
    "expired": "Never filled — the order expired.",
}


def format_pending_notice(
    row: dict,
    *,
    outcome: str,
    spot: float | None = None,
    fill: float | None = None,
    hours: float | None = None,
    reason: str | None = None,
) -> str:
    """Follow up on a resting order. First line is the headline, for bolding."""
    suggestion = Suggestion(
        action=str(row["action"]),
        size=float(row.get("size") or 0.0),
        entry=float(row["entry"]),
        stop_loss=float(row["stop_loss"]),
        take_profits=[],
        risk_reward=row.get("risk_reward"),
        rationale="",
        order_block=None,
        product_id=str(row["product_id"]),
    )
    product = bot_config.product_label(suggestion.product_id)
    title = f"High Quality · {display_summary.friendly_title(suggestion)}"
    entry = float(row["entry"])
    side = display_summary.side_label(suggestion.action)
    headline = _PENDING_HEADLINES.get(outcome, "This order is no longer resting.")

    if outcome == "filled":
        at = f" at ${float(fill):,.2f}" if fill else ""
        body = (
            f"{product} reached the entry, so the limit at ${entry:,.2f} "
            f"executed and the {side} is open{at}. Stop ${float(row['stop_loss']):,.2f}."
        )
    elif outcome == "refused":
        body = (
            f"{product} reached ${entry:,.2f}, but the order was stopped by a "
            f"risk halt or a full sleeve. No position was taken, and the plan "
            f"is not being held for a later fill — that price has passed."
        )
    elif outcome == "missed":
        body = (
            f"{product} ran through both the entry (${entry:,.2f}) and the stop "
            f"(${float(row['stop_loss']):,.2f}) before the order could go on, so "
            f"the setup is spent. No position was taken."
        )
    elif outcome == "expired":
        window = f" within {float(hours):.0f}h" if hours else ""
        body = (
            f"{product} never reached ${entry:,.2f}{window}, so the order has "
            f"been pulled. No position was taken."
        )
    else:  # cancelled
        body = (
            f"Eva re-read the chart and no longer wants this trade, so the "
            f"limit at ${entry:,.2f} has been cancelled"
            + (f" ({reason})" if reason else "")
            + ". No position was taken."
        )

    # A fill already states the price it happened at; repeating the mark reads
    # as a second, different number.
    if spot and outcome != "filled":
        body += f" {product} is ${float(spot):,.2f} now."
    return f"{title} — {headline}\n\n{body}"


async def send_pending_notice_async(
    row: dict, *, outcome: str, **facts: object
) -> int:
    """Answer the card where it was made: forum topic, plus any DM recipients."""
    import live_pending

    recipients = live_pending.recipients_of(row)
    forum = forum_trades_target()
    if not recipients and forum is None:
        return 0

    text = format_pending_notice(row, outcome=outcome, **facts)  # type: ignore[arg-type]
    headline, _, rest = text.partition("\n")
    marked = f"<b>{html.escape(headline)}</b>{html.escape(rest)}"

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    sent = 0
    if forum is not None:
        chat_id, thread_id = forum
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=marked,
                parse_mode=ParseMode.HTML,
                message_thread_id=thread_id,
            )
            sent += 1
        except Exception:
            logger.exception("Pending notice failed for forum %s", chat_id)
    for user_id in recipients:
        try:
            await bot.send_message(
                chat_id=user_id, text=marked, parse_mode=ParseMode.HTML
            )
            sent += 1
        except Exception:
            logger.exception("Pending notice failed for user %s", user_id)
    logger.info(
        "live: %s pending %s notice sent to %d target(s)",
        row.get("product_id"),
        outcome,
        sent,
    )
    return sent


def send_pending_notice(row: dict, *, outcome: str, **facts: object) -> int:
    """Sync wrapper — called from the watchdog sweep thread."""
    return asyncio.run(send_pending_notice_async(row, outcome=outcome, **facts))


def build_rationale_message(suggestion: Suggestion, pnl_footer: str) -> str:
    """Full thesis + Market context + PnL as a follow-up text message (See more)."""
    parts: list[str] = []
    levels = display_summary.build_detail_levels_block(suggestion)
    if suggestion.action != "no_trade":
        parts.append(levels)

    raw = suggestion.rationale.strip()
    if raw:
        header = "NO TRADE" if suggestion.action == "no_trade" else suggestion.action.upper()
        body, context_block = split_rationale(raw)
        why_label = "Why no trade:" if suggestion.action == "no_trade" else "Why this trade:"
        sections = [header]
        if body:
            sections.append(f"{why_label}\n{format_rationale_text(body)}")
        if context_block:
            sections.append(format_rationale_text(context_block))
        elif not body:
            sections.append(f"{why_label}\n{format_rationale_text(raw)}")
        parts.append("\n\n".join(sections))
    if pnl_footer and str(pnl_footer).strip():
        parts.append(str(pnl_footer).strip())
    return "\n\n".join(parts)[:4096]


def _decision_chart_only(chart_paths: list[str] | str) -> list[str]:
    paths = [chart_paths] if isinstance(chart_paths, str) else list(chart_paths)
    paths = [p for p in paths if p and p != "watchdog"]
    decision = [p for p in paths if "decision" in str(p).lower()]
    if decision:
        return decision[:1]
    return paths[:1]


async def send_photo_with_caption(
    bot: Bot,
    chat_id: int | str,
    chart_path: str,
    caption: str,
    *,
    reply_markup=None,
    message_thread_id: int | None = None,
) -> None:
    """Send a chart image with caption to a chat (or forum topic)."""
    path = Path(chart_path)
    if not path.exists():
        raise FileNotFoundError(f"Chart not found: {chart_path}")

    with open(path, "rb") as photo:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption[:1024],
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )


async def send_suggestion_to_chat(
    bot: Bot,
    chat_id: int | str,
    suggestion: Suggestion,
    chart_paths: list[str] | str,
    pnl_footer: str,
    *,
    offer_id: str | None = None,
    telegram_id: int | None = None,
    display_summary_text: str | None = None,
    include_full_rationale: bool = False,
    resting: bool | None = None,
    spot: float | None = None,
    message_thread_id: int | None = None,
    personalize: bool = True,
) -> None:
    """Send the concise decision card; optionally include full detail (ops/resend).

    ``personalize=False`` drops the per-user demo-size copy — a card posted
    once into a shared forum topic cannot name any one person's size.
    """
    paths = _decision_chart_only(chart_paths) if not include_full_rationale else (
        [chart_paths] if isinstance(chart_paths, str) else list(chart_paths[:3])
    )
    paths = [p for p in paths if p and p != "watchdog"]
    tid = telegram_id
    if tid is None and personalize:
        try:
            tid = int(str(chat_id).strip())
        except ValueError:
            tid = None
    if not personalize:
        tid = None

    summary = display_summary_text
    if summary is None and offer_id:
        offer = user_books.get_offer(offer_id)
        if offer and offer.get("display_summary"):
            summary = str(offer["display_summary"])

    caption = build_caption_html(
        suggestion,
        telegram_id=tid,
        offer_id=offer_id,
        display_summary_text=summary,
        resting=resting,
        spot=spot,
    )
    keyboard = (
        telegram_ui.trade_decision_keyboard(offer_id)
        if offer_id and suggestion.action != "no_trade"
        else None
    )

    if not paths:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            message_thread_id=message_thread_id,
        )
        if include_full_rationale:
            rationale_message = build_rationale_message(suggestion, pnl_footer)
            if rationale_message:
                await bot.send_message(
                    chat_id=chat_id,
                    text=rationale_message,
                    message_thread_id=message_thread_id,
                )
        return

    for i, chart_path in enumerate(paths):
        path = Path(chart_path)
        if not path.exists():
            raise FileNotFoundError(f"Chart not found: {chart_path}")

        # The first caption is already-escaped HTML and must not be sliced —
        # see build_caption_html. The rest are ours and contain no markup.
        photo_caption = (
            caption if i == 0 else html.escape(f"Chart {i + 1}/{len(paths)}")
        )
        markup = keyboard if i == 0 else None
        try:
            with open(path, "rb") as photo:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=photo_caption,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=message_thread_id,
                )
        except Exception:
            logger.exception(
                "Photo send failed for chat %s (%s), skipping chart",
                chat_id,
                path.name,
            )
            continue

    if include_full_rationale:
        rationale_message = build_rationale_message(suggestion, pnl_footer)
        if rationale_message:
            await bot.send_message(
                chat_id=chat_id,
                text=rationale_message,
                message_thread_id=message_thread_id,
            )


async def send_offer_details_to_chat(
    bot: Bot,
    chat_id: int | str,
    offer: dict,
    *,
    pnl_footer: str | None = None,
) -> None:
    """See more: structure/entry charts + exact levels + full canonical rationale.

    House-book PnL is omitted — it can describe a different open product and look
    like the wrong trade under the cover card. ``pnl_footer`` is accepted for
    call-site compatibility but ignored.
    """
    del pnl_footer  # See more is offer-scoped only.
    suggestion = user_books.offer_suggestion(offer)
    detail_paths: list[str] = []
    for key in ("structure_chart_path", "entry_chart_path"):
        path = offer.get(key)
        if path and Path(str(path)).exists():
            detail_paths.append(str(path))

    title = display_summary.friendly_title(suggestion)
    for i, chart_path in enumerate(detail_paths):
        caption = f"{title} — detail {i + 1}/{len(detail_paths)}"
        try:
            await send_photo_with_caption(bot, chat_id, chart_path, caption)
        except Exception:
            logger.exception(
                "See more chart send failed for chat %s (%s)", chat_id, chart_path
            )

    text = build_rationale_message(suggestion, pnl_footer="")
    await bot.send_message(chat_id=chat_id, text=text[:4096])


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


async def send_research_report(
    bot: Bot,
    chat_id: int | str,
    report: object,
) -> None:
    """Send a ResearchReport — chart optional."""
    from research_reports.format import ResearchReport

    if not isinstance(report, ResearchReport):
        raise TypeError("report must be a ResearchReport")

    detail = report.detail_text
    if report.chart_path:
        caption = report.caption or report.headline[:1024]
        await send_photo_with_caption(bot, chat_id, report.chart_path, caption)
        if detail:
            await bot.send_message(chat_id=chat_id, text=detail[:4096])
        return

    await bot.send_message(chat_id=chat_id, text=detail[:4096])


async def broadcast_to_subscribers(
    bot: Bot,
    suggestion: Suggestion,
    chart_paths: list[str] | str,
    pnl_footer: str | None = None,
    *,
    offer_id: str | None = None,
    display_summary_text: str | None = None,
    internal_only: bool = False,
    resting: bool | None = None,
    spot: float | None = None,
) -> set[int]:
    """DM the suggestion to every registered subscriber (or allowlist if paywall on).

    internal_only gates the HQ (abstention-first) trade cards to the internal
    ops allowlist instead of the public subscriber list.

    resting is whether the plan was parked as a limit order rather than filled
    at the mark, so the card can say which happened.

    Returns the ids that actually received it. A resting plan is followed up
    when it fills or is pulled, and only this set saw the promise.

    Forum mode (POOL_FORUM_CHAT_ID set, non-internal cards): the card posts
    ONCE into the Trades topic — everyone in the group sees the same message
    and Accept attributes to whoever tapped it. The admin still gets a DM
    copy, and pending follow-ups post back into the same topic.

    When the tester pool is on (``POOL_ENABLED``), trade cards stay personal
    DMs even if a forum is configured — each card needs that user's Accept
    risk and position size on it.
    """
    footer = pnl_footer or paper.format_pnl_footer()

    forum = forum_trades_target()
    if forum is not None and not internal_only and not bot_config.POOL_ENABLED:
        chat_id, thread_id = forum
        sent = set()
        try:
            await send_suggestion_to_chat(
                bot,
                chat_id,
                suggestion,
                chart_paths,
                footer,
                offer_id=offer_id,
                display_summary_text=display_summary_text,
                resting=resting,
                spot=spot,
                message_thread_id=thread_id,
                personalize=False,
            )
            logger.info("Sent suggestion to forum %s topic %s", chat_id, thread_id)
        except Exception:
            logger.exception("Failed to send to forum %s", chat_id)

        admin_chat = config.TELEGRAM_ADMIN_CHAT_ID or config.TELEGRAM_CHAT_ID
        if admin_chat:
            try:
                await send_suggestion_to_chat(
                    bot,
                    admin_chat,
                    suggestion,
                    chart_paths,
                    footer,
                    offer_id=offer_id,
                    display_summary_text=display_summary_text,
                    resting=resting,
                    spot=spot,
                )
            except Exception:
                logger.exception("Failed to send to admin chat %s", admin_chat)
        return sent

    recipients = (
        access.internal_recipient_ids()
        if internal_only
        else access.broadcast_recipient_ids()
    )
    sent = set()

    for user_id in recipients:
        if user_id in sent:
            continue
        try:
            await send_suggestion_to_chat(
                bot,
                user_id,
                suggestion,
                chart_paths,
                footer,
                offer_id=offer_id,
                telegram_id=user_id,
                display_summary_text=display_summary_text,
                resting=resting,
                spot=spot,
            )
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
                await send_suggestion_to_chat(
                    bot,
                    admin_chat,
                    suggestion,
                    chart_paths,
                    footer,
                    offer_id=offer_id,
                    telegram_id=admin_id,
                    display_summary_text=display_summary_text,
                    resting=resting,
                    spot=spot,
                )
                sent.add(admin_id)
                logger.info("Sent suggestion to admin chat %s", admin_chat)
            except Exception:
                logger.exception("Failed to send to admin chat %s", admin_chat)

    return sent


def format_audit_alert(verdict: AuditVerdict) -> str:
    """Format monitor chat alert for deterministic + LLM findings (chat audits)."""
    if verdict.source == "hourly":
        header = f"AUDIT — cycle {verdict.cycle_id or 'n/a'}"
        if verdict.action:
            header += f" | {verdict.action}"
    else:
        header = f"CHAT AUDIT — user {verdict.user_id or 'n/a'}"
        if verdict.cycle_id:
            header += f" | snapshot {verdict.cycle_id}"

    lines = [header, ""]
    if verdict.sanitized:
        lines.append("Note: LLM rationale was replaced with sanitized summary before broadcast.")
        lines.append("")
    if verdict.deterministic:
        lines.append("[DETERMINISTIC]")
        for finding in verdict.deterministic:
            mark = "!" if finding.severity == "critical" else "?"
            lines.append(f"{mark} {finding.code}: {finding.message}")
        lines.append("")

    if verdict.llm_hallucinations:
        lines.append("[LLM CRITIC]")
        for finding in verdict.llm_hallucinations:
            lines.append(f"! {finding.code}: {finding.message}")
        lines.append("")

    if verdict.text_excerpt:
        lines.append(f'Excerpt: "{verdict.text_excerpt}"')

    return "\n".join(lines)[:4096]


def format_hourly_monitor_report(verdict: AuditVerdict, *, broadcast_sent: bool) -> str:
    """Full hourly assessment for MONITOR_CHAT_ID — sent every cycle."""
    action = (verdict.action or "unknown").upper()
    header = f"HOURLY MONITOR — cycle {verdict.cycle_id or 'n/a'} | {action}"
    lines = [header, ""]

    if verdict.sanitized:
        lines.append("Pre-broadcast: rationale was sanitized after audit failures.")
        lines.append("")
    if verdict.downgraded:
        lines.append("Pre-broadcast: trade action downgraded to no_trade after audit failures.")
        lines.append("")
    if verdict.passes_used:
        lines.append(f"Refine passes used: {verdict.passes_used}")
        lines.append("")

    if verdict.score is not None:
        bd = verdict.score_breakdown or {}
        lines.append(
            f"Chart-read score: {verdict.score}/100 "
            f"(critical={bd.get('critical', 0)}, warnings={bd.get('warning', 0)}, "
            f"hallucinations={bd.get('llm_hallucinations', 0)}, "
            f"verified={bd.get('verified_claims', 0)})"
        )
        lines.append("")

    if broadcast_sent:
        lines.append("Subscriber broadcast: sent")
    else:
        reason = "no_trade" if action == "NO_TRADE" else "skipped"
        lines.append(f"Subscriber broadcast: skipped ({reason})")
    lines.append("")

    critical = [f for f in verdict.deterministic if f.severity == "critical"]
    warnings = [f for f in verdict.deterministic if f.severity == "warning"]

    lines.append("[DETERMINISTIC — pass 1]")
    if critical:
        for finding in critical:
            lines.append(f"! {finding.code}: {finding.message}")
    elif warnings:
        lines.append("✓ No critical deterministic issues.")
    else:
        lines.append("✓ All deterministic fact-checks passed.")
    if warnings:
        lines.append("")
        lines.append("[WARNINGS]")
        for finding in warnings:
            lines.append(f"? {finding.code}: {finding.message}")
    lines.append("")

    lines.append("[LLM CRITIC — pass 2]")
    if verdict.llm_hallucinations:
        for finding in verdict.llm_hallucinations:
            lines.append(f"! {finding.code}: {finding.message}")
    else:
        lines.append("✓ No hallucinations flagged.")
    if verdict.llm_verified:
        lines.append("")
        lines.append("[VERIFIED CLAIMS]")
        for claim in verdict.llm_verified:
            lines.append(f"✓ {claim}")
    lines.append("")

    if verdict.text_excerpt:
        lines.append(f'Rationale excerpt: "{verdict.text_excerpt}"')

    return "\n".join(lines)[:4096]


async def send_hourly_monitor_report_async(
    verdict: AuditVerdict,
    *,
    broadcast_sent: bool,
) -> None:
    """Post full hourly assessment to MONITOR_CHAT_ID (every cycle)."""
    chat_id = config.MONITOR_CHAT_ID
    if not chat_id:
        logger.debug("MONITOR_CHAT_ID not set — skipping hourly monitor report")
        return

    text = format_hourly_monitor_report(verdict, broadcast_sent=broadcast_sent)
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=int(str(chat_id).strip()), text=text)


def send_hourly_monitor_report(verdict: AuditVerdict, *, broadcast_sent: bool) -> None:
    """Sync wrapper for agent cycle."""
    try:
        asyncio.run(send_hourly_monitor_report_async(verdict, broadcast_sent=broadcast_sent))
    except Exception:
        logger.exception("Failed to send hourly monitor report")


async def send_monitor_alert_async(verdict: AuditVerdict) -> None:
    """Post audit findings to MONITOR_CHAT_ID when issues are found."""
    if not verdict.has_issues:
        return
    chat_id = config.MONITOR_CHAT_ID
    if not chat_id:
        logger.debug("MONITOR_CHAT_ID not set — skipping audit alert")
        return

    text = format_audit_alert(verdict)
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=int(str(chat_id).strip()), text=text)


def send_monitor_alert(verdict: AuditVerdict) -> None:
    """Sync wrapper for agent cycle / chat executor."""
    if not verdict.has_issues:
        return
    try:
        asyncio.run(send_monitor_alert_async(verdict))
    except Exception:
        logger.exception("Failed to send monitor audit alert")


def broadcast(
    suggestion: Suggestion,
    chart_paths: list[str] | str,
    pnl_footer: str | None = None,
    *,
    offer_id: str | None = None,
    display_summary_text: str | None = None,
    internal_only: bool = False,
    resting: bool | None = None,
    spot: float | None = None,
) -> set[int]:
    """Sync wrapper for standalone agent.py / tests. Returns the ids reached."""
    footer = pnl_footer or paper.format_pnl_footer()

    async def _run() -> set[int]:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        return await broadcast_to_subscribers(
            bot,
            suggestion,
            chart_paths,
            footer,
            offer_id=offer_id,
            display_summary_text=display_summary_text,
            internal_only=internal_only,
            resting=resting,
            spot=spot,
        )

    return asyncio.run(_run())


def broadcast_text(
    suggestion: Suggestion,
    pnl_footer: str | None = None,
    *,
    offer_id: str | None = None,
    display_summary_text: str | None = None,
    resting: bool | None = None,
    spot: float | None = None,
) -> set[int]:
    """Broadcast a watchdog / text-only trade signal (no chart images)."""
    footer = pnl_footer or paper.format_pnl_footer()

    async def _run() -> set[int]:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        return await broadcast_to_subscribers(
            bot,
            suggestion,
            [],
            footer,
            offer_id=offer_id,
            display_summary_text=display_summary_text,
            resting=resting,
            spot=spot,
        )

    return asyncio.run(_run())


async def broadcast_plain_text_async(text: str) -> None:
    """Research-labelled pushes (z-moves, digests): forum Research topic when
    configured, otherwise DM every broadcast recipient as before."""
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    body = text.strip()[:4096]

    forum = forum_research_target()
    if forum is not None:
        chat_id, thread_id = forum
        try:
            await bot.send_message(
                chat_id=chat_id, text=body, message_thread_id=thread_id
            )
        except Exception:
            logger.exception("Failed to send plain broadcast to forum %s", chat_id)
        admin_chat = config.TELEGRAM_ADMIN_CHAT_ID or config.TELEGRAM_CHAT_ID
        if admin_chat:
            try:
                await bot.send_message(chat_id=admin_chat, text=body)
            except Exception:
                logger.exception("Plain broadcast admin copy failed")
        return

    recipients = access.broadcast_recipient_ids()
    sent: set[int] = set()
    for user_id in recipients:
        if user_id in sent:
            continue
        try:
            await bot.send_message(chat_id=user_id, text=body)
            sent.add(user_id)
        except Exception:
            logger.exception("Failed to send plain broadcast to user %s", user_id)

    admin_chat = config.TELEGRAM_ADMIN_CHAT_ID or config.TELEGRAM_CHAT_ID
    if admin_chat:
        try:
            admin_id = int(str(admin_chat).strip())
        except ValueError:
            admin_id = None
        if admin_id is not None and admin_id not in sent:
            try:
                await bot.send_message(chat_id=admin_id, text=body)
            except Exception:
                logger.exception("Failed to send plain broadcast to admin %s", admin_chat)


def broadcast_plain_text(text: str) -> None:
    """Sync wrapper for Z-Move / advisory plain-text subscriber DMs."""

    async def _run() -> None:
        await broadcast_plain_text_async(text)

    asyncio.run(_run())


async def send_missed_connection_async(target: dict) -> None:
    """DM late-join invite to users who rejected/expired an offer."""
    offer_id = target["offer_id"]
    chart = target.get("decision_chart_path")
    r_mult = float(target.get("r_multiple") or 0)
    spot = float(target.get("spot") or 0)
    product = bot_config.product_label(str(target.get("product_id") or "ETH-USD"))
    text = (
        f"Missed connection — {product} is running ≈ {r_mult:+.2f}R "
        f"(mark ${spot:,.2f}).\n\n"
        "Join at the current mark with the same SL/TP levels, or stay out."
    )
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    keyboard = telegram_ui.missed_connection_keyboard(offer_id)
    for tid in target.get("telegram_ids") or []:
        try:
            if chart and Path(str(chart)).exists():
                await send_photo_with_caption(
                    bot, tid, str(chart), text, reply_markup=keyboard
                )
            else:
                await bot.send_message(
                    chat_id=tid, text=text[:4096], reply_markup=keyboard
                )
        except Exception:
            logger.exception("Missed-connection DM failed for %s", tid)
    user_books.mark_missed_connection_sent(offer_id)


def process_missed_connections(spots: dict[str, float] | None = None) -> int:
    """Find +0.5R house opens and send one missed-connection DM each. Returns DMs."""
    targets = user_books.find_missed_connection_targets(spots=spots)
    if not targets:
        return 0
    sent = 0
    for target in targets:
        try:
            asyncio.run(send_missed_connection_async(target))
            sent += 1
        except Exception:
            logger.exception(
                "Missed-connection processing failed for %s",
                target.get("offer_id"),
            )
    return sent


async def send_launch_notice_async() -> None:
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    text = user_books.LAUNCH_NOTICE
    for user_id in access.broadcast_recipient_ids():
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text[:4096],
                reply_markup=telegram_ui.main_keyboard(),
            )
        except Exception:
            logger.exception("Launch notice failed for %s", user_id)


def maybe_send_launch_notice() -> bool:
    """Send one-time personal-books launch notice. Returns True if sent."""
    key = bot_config.LAUNCH_NOTICE_SENT_KEY
    if user_books.get_meta(key) == "1":
        return False
    try:
        asyncio.run(send_launch_notice_async())
        user_books.set_meta(key, "1")
        return True
    except Exception:
        logger.exception("Launch notice broadcast failed")
        return False


def send_watchdog_monitor_alert(
    cycle_id: str,
    trigger_name: str,
    suggestion: Suggestion,
) -> None:
    """Notify MONITOR_CHAT_ID when the watchdog fires a programmatic entry."""
    chat_id = config.MONITOR_CHAT_ID
    if not chat_id:
        return
    tps = ", ".join(f"{tp:,.2f}" for tp in suggestion.take_profits[:3]) or "n/a"
    rr = f"{suggestion.risk_reward:.2f}" if suggestion.risk_reward is not None else "n/a"
    text = (
        f"WATCHDOG ENTRY — cycle {cycle_id}\n"
        f"Trigger: {trigger_name}\n"
        f"Action: {suggestion.action}\n"
        f"Entry: {suggestion.entry:,.2f} | SL: {suggestion.stop_loss:,.2f} | "
        f"TP: {tps} | R/R: {rr}\n"
        f"(programmatic — no chart review this cycle)"
    )

    async def _run() -> None:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=int(str(chat_id).strip()), text=text[:4096])

    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("Failed to send watchdog monitor alert")


def send_macro_pulse_alert(
    event: dict,
    advisory: dict,
    text_summary: str,
) -> None:
    """Notify MONITOR_CHAT_ID of a high-severity macro pulse advisory."""
    chat_id = config.MONITOR_CHAT_ID
    if not chat_id:
        return
    rec = advisory.get("recommendation", "hold")
    text = (
        f"MACRO PULSE — severity {event.get('severity')} ({event.get('eth_bias')})\n"
        f"{event.get('title', '')}\n\n"
        f"Recommendation: {rec}\n"
        f"{text_summary}\n\n"
        f"(advisory only — no auto-trade)"
    )
    if event.get("url"):
        text += f"\n{event['url']}"

    async def _run() -> None:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=int(str(chat_id).strip()), text=text[:4096])

    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("Failed to send macro pulse alert")


# ---------------------------------------------------------------------------
# Tester pool — Account-lane DMs (personal money stays out of the forum)
# ---------------------------------------------------------------------------

async def send_pool_dm_async(telegram_id: int, text: str) -> bool:
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(chat_id=int(telegram_id), text=text.strip()[:4096])
        return True
    except Exception:
        logger.exception("Pool DM failed for user %s", telegram_id)
        return False


def send_pool_dm(telegram_id: int, text: str) -> bool:
    """Sync wrapper — safe from executor/watchdog threads. Never raises."""
    try:
        return asyncio.run(send_pool_dm_async(telegram_id, text))
    except Exception:
        logger.exception("Pool DM wrapper failed for user %s", telegram_id)
        return False


def send_pool_admin_alert(text: str) -> None:
    """Alert every pool admin by DM. Never raises."""
    import pool

    async def _run() -> None:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        for admin_id in pool.admin_ids():
            try:
                await bot.send_message(chat_id=admin_id, text=text.strip()[:4096])
            except Exception:
                logger.exception("Pool admin alert failed for %s", admin_id)

    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("Pool admin alert failed")


def _latest_output_chart() -> Path:
    for pattern in ("*_entry.png", "*_structure.png", "*_notrade.png", "*_M5_annotated.png"):
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
