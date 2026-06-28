#!/usr/bin/env python3
"""Re-send a ledger suggestion to Telegram (text-only if chart fails)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import access
import config
import ledger
import notify
import paper
from models import Suggestion
from telegram import Bot


def _load_suggestion(cycle_id: str, note: str | None) -> tuple[Suggestion, str]:
    row = ledger.get_suggestion_by_cycle_id(cycle_id)
    if row is None:
        raise SystemExit(f"No suggestion found for cycle_id={cycle_id!r}")

    rationale = row.get("rationale") or ""
    if note:
        rationale = f"{rationale.rstrip()}\n\n{note}".strip()

    suggestion = Suggestion(
        action=row["action"],
        size=float(row["size"] or 0),
        entry=row["entry"],
        stop_loss=row["stop_loss"],
        take_profits=row["take_profits"],
        risk_reward=row["risk_reward"],
        rationale=rationale,
    )
    chart = row.get("chart_path") or ""
    return suggestion, chart


async def _send_text(bot: Bot, user_id: int, suggestion: Suggestion, footer: str) -> None:
    caption = notify.build_caption(suggestion)
    body = notify.build_rationale_message(suggestion, footer)
    await bot.send_message(chat_id=user_id, text=f"{caption}\n\n{body}"[:4096])


async def _send_full(
    bot: Bot,
    user_id: int,
    suggestion: Suggestion,
    chart_path: str,
    footer: str,
    text_only: bool,
) -> None:
    if text_only:
        await _send_text(bot, user_id, suggestion, footer)
        return
    try:
        await notify.send_suggestion_to_chat(bot, user_id, suggestion, chart_path, footer)
    except Exception:
        print(f"  photo failed for {user_id}, falling back to text")
        await _send_text(bot, user_id, suggestion, footer)


async def _run(cycle_id: str, text_only: bool, note: str | None) -> int:
    suggestion, chart = _load_suggestion(cycle_id, note)
    footer = paper.format_pnl_footer()
    recipients = access.broadcast_recipient_ids()

    print(f"Cycle:   {cycle_id}")
    print(f"Action:  {suggestion.action} @ {suggestion.entry}")
    print(f"Chart:   {chart}")
    print(f"Mode:    {'text-only' if text_only else 'photo (text fallback)'}")
    print(f"Sending to {len(recipients)} recipient(s): {recipients}")

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    ok = 0
    for user_id in recipients:
        try:
            await _send_full(bot, user_id, suggestion, chart, footer, text_only)
            print(f"  OK  {user_id}")
            ok += 1
        except Exception as exc:
            print(f"  FAIL {user_id}: {exc}")

    print(f"Sent to {ok}/{len(recipients)} recipient(s).")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-send a ledger suggestion via Telegram")
    parser.add_argument("cycle_id", help="e.g. 20260627T012825Z")
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="Skip chart photo (use when Telegram rejects Photo_invalid_dimensions)",
    )
    parser.add_argument(
        "--note",
        default="(Resent manually — original broadcast failed.)",
        help="Appended to rationale",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.cycle_id, args.text_only, args.note)))


if __name__ == "__main__":
    main()
