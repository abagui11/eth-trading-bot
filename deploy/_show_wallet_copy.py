"""Read-only: render the tester-facing wallet and deposit copy."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import telegram_ui  # noqa: E402

W = "0x1111111111111111111111111111111111111111"
BAR = "\n" + "=" * 68 + "\n"

print(f"POOL_DEPOSIT_ADDRESS = {config.POOL_DEPOSIT_ADDRESS}")
print(f"POOL_DEPOSIT_CHAIN   = {config.POOL_DEPOSIT_CHAIN}")

print(BAR + "/deposit  — no wallet registered yet" + BAR)
print(telegram_ui.format_deposit_instructions(wallet=None))

print(BAR + "/deposit  — wallet registered" + BAR)
print(telegram_ui.format_deposit_instructions(wallet=W))

print(BAR + "/wallet  — nothing registered" + BAR)
print(telegram_ui.format_wallet_status(None))

print(BAR + "/wallet  — registered, unproven" + BAR)
print(telegram_ui.format_wallet_status({"address": W, "status": "pending"}))

print(BAR + "/wallet  — verified" + BAR)
print(telegram_ui.format_wallet_status({"address": W, "status": "verified"}))

print(BAR + "/wallet  — verified, in post-change cooldown" + BAR)
print(telegram_ui.format_wallet_status(
    {"address": W, "status": "verified",
     "payouts_blocked_until": "2026-09-16T20:00:00Z"},
    change={"address": "0x2222222222222222222222222222222222222222"},
))
