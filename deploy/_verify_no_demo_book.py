"""Confirm the demo paper book is unreachable for live accounts.

Run on the server after deploying. Reads only.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402
import bot_config  # noqa: E402
import config  # noqa: E402
import pool  # noqa: E402
import telegram_ui  # noqa: E402
import trade_ideas_bridge as bridge  # noqa: E402
import user_books  # noqa: E402

DEMO_TAPS = (
    telegram_ui.CB_OPEN,
    telegram_ui.CB_METRICS,
    telegram_ui.CB_MY_BOOK,
    f"{telegram_ui.CB_OPEN_SIZE_PREFIX}2500",
    f"{telegram_ui.CB_TRADE_JOIN_PREFIX}offer-1",
    f"{telegram_ui.CB_TRADE_SKIP_PREFIX}offer-1",
)

LIVE_TAPS = (
    f"{telegram_ui.CB_TRADE_YES_PREFIX}offer-1",
    f"{telegram_ui.CB_TRADE_NO_PREFIX}offer-1",
    telegram_ui.CB_POOL_PORTFOLIO,
    telegram_ui.CB_FEED,
)

fails = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global fails
    if not ok:
        fails += 1
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def main() -> int:
    print(f"POOL_ENABLED = {bot_config.POOL_ENABLED}")

    print("\nbutton menu")
    labels = [b.text for row in telegram_ui.main_keyboard().inline_keyboard
              for b in row]
    print(f"  {labels}")
    check("no Open account", "Open account" not in labels)
    check("no My book", "My book" not in labels)
    check("has Portfolio", "Portfolio" in labels)
    check("has Deposit", "Deposit" in labels)

    print("\ndemo taps refused (old keyboards stay tappable in scrollback)")
    for data in DEMO_TAPS:
        check(data, bot._is_legacy_paper(data))

    print("\nlive taps untouched")
    for data in LIVE_TAPS:
        check(data, not bot._is_legacy_paper(data))

    print("\naccept copy")
    reply = bridge.format_decision_reply("recorded", "accept", 1027)
    print(f"  {reply!r}")
    check("does not claim a paper book", "paper" not in reply.lower())

    print("\napproved accounts are treated as live")
    accounts = pool.list_accounts()
    for row in accounts[:12]:
        tid = int(row["telegram_id"])
        live = bool(bot_config.POOL_ENABLED) and pool.is_approved(tid)
        demo = user_books.get_account(tid)
        note = ""
        if demo is not None:
            note = (f"legacy demo book ${float(demo['starting_usd']):,.0f} "
                    f"(opened {demo['opened_at'][:10]}, now unreachable)")
        print(f"  {tid}  live={live}  cash=${float(row['cash_usd']):,.2f}  {note}")
        check(f"{tid} routed to live product", live)

    print(f"\n{'all checks passed' if not fails else f'{fails} FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
