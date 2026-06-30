#!/usr/bin/env python3
"""One-off: announcement DM + re-send a missed trade suggestion to all subscribers."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import access
import charts
import config
import ledger
import notify
import paper
import research
from models import Suggestion
from telegram import Bot

MISSED_CYCLE_ID = "20260627T012825Z"

ANNOUNCEMENT = """\
hi, this is Arpan, designer of this bot. there was an error on July 27th 1:29pm when the bot decided on a short trade (all paper orders), but the message was not broadcast on telegram due to an error with how trades affect the chart dimensions. this has been fixed going forward. for details about the trade that was missed, refer to the below.

updates should resume as normal\
"""


def _load_suggestion(cycle_id: str) -> Suggestion:
    row = ledger.get_suggestion_by_cycle_id(cycle_id)
    if row is None:
        raise SystemExit(f"No suggestion found for cycle_id={cycle_id!r}")

    return Suggestion(
        action=row["action"],
        size=float(row["size"] or 0),
        entry=row["entry"],
        stop_loss=row["stop_loss"],
        take_profits=row["take_profits"],
        risk_reward=row["risk_reward"],
        rationale=(row["rationale"] or "").strip(),
    )


def _regenerate_chart(suggestion: Suggestion, cycle_id: str) -> str:
    """Re-render annotated chart with current charts.py (fixes broken PNG on disk)."""
    h1_bars = research.get_ohlc("H1")
    render_cycle = f"{cycle_id}_resent"
    h1_stub = str(config.CHARTS_DIR / f"{cycle_id}_H1.png")
    return charts.annotate_chart(
        h1_stub,
        suggestion,
        render_cycle,
        h1_bars=h1_bars,
    )


async def _run(dry_run: bool) -> int:
    suggestion = _load_suggestion(MISSED_CYCLE_ID)
    recipients = access.broadcast_recipient_ids()
    footer = paper.format_pnl_footer()

    print(f"Missed trade: {suggestion.action} @ {suggestion.entry} (cycle {MISSED_CYCLE_ID})")
    print(f"Recipients ({len(recipients)}): {recipients}")

    if dry_run:
        print("Dry run — nothing sent.")
        chart = _regenerate_chart(suggestion, MISSED_CYCLE_ID)
        print(f"Would use chart: {chart}")
        return 0

    chart_path = _regenerate_chart(suggestion, MISSED_CYCLE_ID)
    print(f"Regenerated chart: {chart_path}")

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    ok = 0

    for user_id in recipients:
        try:
            await bot.send_message(chat_id=user_id, text=ANNOUNCEMENT[:4096])
            await notify.send_suggestion_to_chat(
                bot, user_id, suggestion, chart_path, footer
            )
            print(f"  OK  {user_id}")
            ok += 1
        except Exception as exc:
            print(f"  FAIL {user_id}: {exc}")

    print(f"Sent announcement + trade to {ok}/{len(recipients)} recipient(s).")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Broadcast Arpan announcement + missed deriv_sell trade"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show recipients and chart path without sending",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.dry_run)))


if __name__ == "__main__":
    main()
