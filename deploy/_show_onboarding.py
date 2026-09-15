"""Print every message a new tester sees, in the order they hit them.

For briefing testers and for reviewing copy without having to drive a real
Telegram account through the whole flow. Reads config only; touches nothing.

    sudo -u ethagent .venv/bin/python deploy/_show_onboarding.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config    # noqa: E402
import config        # noqa: E402
import telegram_ui   # noqa: E402

WALLET = "0xYOURWALLET00000000000000000000000000beef"


def show(step: str, sends: str, text: str) -> None:
    print("\n" + "=" * 70)
    print(f"{step}   [user sends: {sends}]")
    print("=" * 70)
    print(text)


def main() -> int:
    print(f"deposit address : {config.POOL_DEPOSIT_ADDRESS}")
    print(f"network         : {config.POOL_DEPOSIT_CHAIN}")
    print(f"min deposit     : ${bot_config.POOL_MIN_DEPOSIT_USD:,.0f}")
    print(f"min withdrawal  : ${bot_config.POOL_MIN_WITHDRAWAL_USD:,.0f}")
    print(f"risk per Accept : {bot_config.POOL_RISK_PCT * 100:.1f}% of available cash")

    show("1. First contact, before admin admits them", "/start",
         telegram_ui.PENDING_APPROVAL_MESSAGE)

    show("2. After the admin taps Admit", "/start (or automatic on Admit)",
         telegram_ui.POOL_WELCOME_MESSAGE)

    show("3. Funding, before any wallet is registered", "/deposit",
         telegram_ui.format_deposit_instructions(wallet=None))

    show("4. Wallet not yet registered", "/wallet",
         telegram_ui.format_wallet_status(None))

    show("5. Funding, once a wallet is registered", "/deposit",
         telegram_ui.format_deposit_instructions(wallet=WALLET))

    show("6. Wallet registered but not yet proven", "/wallet",
         telegram_ui.format_wallet_status(
             {"address": WALLET, "status": "pending",
              "payouts_blocked_until": None}))

    show("7. Wallet proven by an arriving deposit", "/wallet",
         telegram_ui.format_wallet_status(
             {"address": WALLET, "status": "verified",
              "payouts_blocked_until": None}))

    print("\n" + "=" * 70)
    print("8. Automatic messages (no command — these arrive on their own)")
    print("=" * 70)
    print("""
  On deposit settling (within ~a minute of arrival):
    Deposit received: $1,000.00 USDC.
    Cash balance: $1,000.00.
    This was confirmed automatically against the exchange, not by hand.
    You can Accept trade cards now — /portfolio any time.

  Shortly after, once the sender is checked on-chain:
    Your wallet is verified.
    0x...
    We confirmed on-chain that your deposit came from this address, so it
    is the only place withdrawals can go. Use /withdraw whenever you like.

  On a withdrawal being sent:
    Withdrawal sent: $100.00 USDC.  To: 0x...  Network fee: $0.15.
    It is on its way — usually a few minutes. We will message you again
    with the transaction once it lands in your wallet.

  When it lands (confirmed on-chain, not assumed):
    Withdrawal confirmed: $100.00 USDC has landed in 0x...
    Transaction: 0x...
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
