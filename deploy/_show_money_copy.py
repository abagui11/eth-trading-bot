"""Print the deposit and withdrawal messages a tester actually reads.

Read-only. Sends nothing, moves nothing.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config  # noqa: E402
import config  # noqa: E402
import telegram_ui  # noqa: E402

WALLET = "0x1111111111111111111111111111111111111111"


def show(title: str, text: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    for line in str(text).splitlines():
        print(f"  {line}")


def main() -> int:
    print(f"POOL_AUTO_APPROVE_WITHDRAWALS = "
          f"{bot_config.POOL_AUTO_APPROVE_WITHDRAWALS}")
    print(f"deposit address {config.POOL_DEPOSIT_ADDRESS} "
          f"on {config.POOL_DEPOSIT_CHAIN}")

    show("/deposit — before a wallet is registered",
         telegram_ui.format_deposit_instructions())
    show("/deposit — wallet registered",
         telegram_ui.format_deposit_instructions(wallet=WALLET))

    request_id = 7
    show("/deposit 1000 0x… — the claim is filed", (
        f"Got it — watching the exchange for that transfer (#{request_id}).\n\n"
        "Give it about 5 minutes. That's the Ethereum confirmations plus the "
        "exchange crediting it; a busy network can make it longer.\n\n"
        "You'll be credited automatically the moment it settles, and I'll "
        "message you here with your new balance. Nothing else for you to do — "
        "you don't need to wait on this screen."
    ))

    auto = bool(bot_config.POOL_AUTO_APPROVE_WITHDRAWALS)
    timing = (
        "It goes out on the next payout pass — usually within a minute or "
        "two, and on-chain a minute or so after that. Call it under five "
        "minutes end to end."
        if auto else
        "It's queued for a final check before sending."
    )
    show(f"/withdraw 100 — {'auto-approved' if auto else 'admin-gated'}", (
        f"Withdrawal #3 {'approved' if auto else 'queued'}: $100.00\n"
        f"To: {WALLET}\n\n"
        "$103.00 is held from your balance (the extra covers the network "
        "fee; anything unused comes back).\n\n"
        f"{timing}\n\n"
        "I'll message you when it's sent, and again when it lands in your "
        "wallet. You don't need to wait here."
    ))

    print(f"\n{'=' * 70}\nlimits in force\n{'=' * 70}")
    for name in (
        "POOL_MIN_WITHDRAWAL_USD", "POOL_MAX_WITHDRAWAL_USD",
        "POOL_MAX_USER_DAILY_WITHDRAWAL_USD",
        "POOL_MAX_GLOBAL_DAILY_WITHDRAWAL_USD",
        "POOL_WITHDRAWAL_FEE_RESERVE_USD",
        "POOL_PAYOUTS_ENABLED", "POOL_REQUIRE_VERIFIED_WALLET",
    ):
        print(f"  {name:<38} {getattr(bot_config, name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
