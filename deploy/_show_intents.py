"""What happened to recent Accepts.

Answers "the tester pressed Accept and never heard back" by showing the intent
rows, their status, and what the sweep currently considers an active ref.
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def main() -> int:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row

    print("=== pool_intents (latest 25) ===")
    for r in conn.execute(
        "select id, ref, telegram_id, status, risk_usd, created_at"
        " from pool_intents order by id desc limit 25"
    ):
        print(f"  #{r['id']:<4} {str(r['ref']):<28} uid {r['telegram_id']} "
              f"{str(r['status']):<9} ${float(r['risk_usd']):>7,.2f}  "
              f"{r['created_at']}")

    print("\n=== accounts ===")
    for r in conn.execute(
        "select telegram_id, cash_usd, reserved_usd from pool_accounts"
        " where cash_usd > 0 or reserved_usd > 0"
    ):
        print(f"  {r['telegram_id']}  cash ${float(r['cash_usd']):,.2f} "
              f"reserved ${float(r['reserved_usd']):,.2f}")

    print("\n=== what the sweep thinks is still live ===")
    import live_pending
    import trade_ideas_bridge
    pend = {str(r.get("cycle_id") or "") for r in live_pending.get_pending()}
    mill = trade_ideas_bridge.pool_active_mill_refs()
    print(f"  live_pending cycle ids : {sorted(pend)}")
    print(f"  active mill refs       : {sorted(mill)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
