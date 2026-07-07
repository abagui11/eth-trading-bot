"""Telegram delivery: per-subscriber DMs with chart, rationale, and PnL footer."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Bot

import access
import config
import paper
from critic import AuditVerdict
from models import Suggestion

logger = logging.getLogger(__name__)

# TODO: inline approve/reject buttons + APPROVAL_WINDOW_MIN timeout (full build).


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


def build_caption(suggestion: Suggestion) -> str:
    """Short caption for the chart photo (Telegram limit: 1024 characters)."""
    if suggestion.action == "no_trade":
        return "NO TRADE — rationale in the message below."

    tps = ", ".join(f"{tp:,.2f}" for tp in suggestion.take_profits[:3]) or "n/a"
    rr = f"{suggestion.risk_reward:.2f}" if suggestion.risk_reward is not None else "n/a"
    prefix = ""
    if suggestion.rationale.strip().startswith("[Watchdog"):
        prefix = "WATCHDOG — "
    return (
        f"{prefix}{suggestion.action.upper()}\n"
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
        formatted = format_rationale_text(suggestion.rationale)
        parts.append(f"{header}\n\nRationale:\n{formatted}")
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
    paths = [p for p in paths if p and p != "watchdog"]
    caption = build_caption(suggestion)
    rationale_message = build_rationale_message(suggestion, pnl_footer)

    if not paths:
        text = f"{caption}\n\n{rationale_message}" if caption else rationale_message
        await bot.send_message(chat_id=chat_id, text=text[:4096])
        return

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
) -> None:
    """Sync wrapper for standalone agent.py / tests."""
    footer = pnl_footer or paper.format_pnl_footer()

    async def _run() -> None:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await broadcast_to_subscribers(bot, suggestion, chart_paths, footer)

    asyncio.run(_run())


def broadcast_text(
    suggestion: Suggestion,
    pnl_footer: str | None = None,
) -> None:
    """Broadcast a watchdog / text-only trade signal (no chart images)."""
    footer = pnl_footer or paper.format_pnl_footer()

    async def _run() -> None:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await broadcast_to_subscribers(bot, suggestion, [], footer)

    asyncio.run(_run())


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
