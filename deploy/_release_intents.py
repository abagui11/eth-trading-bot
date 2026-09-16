"""Release specific pending intents and DM their owners.

For clearing reserves stranded by a bug, where waiting out the normal window
is not the point. Sends the same DM the watchdog's stale-intent sweep sends,
so the tester's experience is identical to the automatic path.

    python deploy/_release_intents.py mill_1024 mill_1025
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402
import pool    # noqa: E402


def main() -> int:
    refs = sys.argv[1:]
    if not refs:
        print(__doc__)
        return 2

    for ref in refs:
        released = pool.release_intents(ref)
        if not released:
            print(f"{ref}: nothing pending")
            continue
        for intent in released:
            uid = int(intent["telegram_id"])
            risk = float(intent["risk_usd"])
            ok = notify.send_pool_dm(
                uid,
                "That order never fired — it was replaced, expired, or the "
                f"setup passed. Your ${risk:,.2f} is back in your available "
                "balance. Nothing was risked.",
            )
            print(f"{ref}: released ${risk:,.2f} to {uid} (dm={'ok' if ok else 'FAILED'})")

    print("\n--- balances now ---")
    for account in pool.list_accounts():
        cash = float(account["cash_usd"])
        reserved = float(account["reserved_usd"])
        if cash or reserved:
            print(f"  {account['telegram_id']}  cash ${cash:,.2f} "
                  f"reserved ${reserved:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
