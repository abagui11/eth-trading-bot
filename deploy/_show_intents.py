"""Show pool intents and what the sweep currently considers fillable.

Read-only. Answers "why is this tester's money reserved, and will it come
back on its own?"
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config  # noqa: E402
import config  # noqa: E402
import pool  # noqa: E402
import trade_ideas_bridge as bridge  # noqa: E402


def age_min(ts: str) -> float:
    try:
        then = datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return -1.0
    return (datetime.now(timezone.utc) - then).total_seconds() / 60.0


def main() -> int:
    ttl = float(bot_config.POOL_INTENT_TTL_MIN)
    active = set(bridge.pool_active_mill_refs())

    with pool._connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pool_intents ORDER BY id DESC LIMIT 25"
        )]

    print(f"POOL_INTENT_TTL_MIN = {ttl:.0f} min")
    print(f"active mill refs the sweep will not release: "
          f"{sorted(active) if active else '(none)'}\n")

    print(f"{'id':>4}  {'ref':<14} {'who':>12} {'risk':>7} {'att':>3} "
          f"{'status':<9} {'age':>7}  verdict")
    for r in rows:
        ref = str(r["ref"])
        age = age_min(r["created_at"])
        if r["status"] != "pending":
            verdict = "done"
        elif ref in active:
            verdict = (f"held (still fillable); TTL frees it in "
                       f"{max(0.0, ttl - age):.0f} min")
        else:
            verdict = "next sweep releases it"
        print(f"{r['id']:>4}  {ref:<14} {r['telegram_id']:>12} "
              f"${float(r['risk_usd']):>6.2f} {int(r.get('attempt') or 1):>3} "
              f"{r['status']:<9} {age:>6.0f}m  {verdict}")

    print("\naccounts with money set aside")
    for acct in pool.list_accounts():
        reserved = float(acct["reserved_usd"])
        if reserved <= 0:
            continue
        print(f"  {acct['telegram_id']}  cash ${float(acct['cash_usd']):,.2f}  "
              f"reserved ${reserved:,.2f}  "
              f"withdrawable ${pool.withdrawable_usd(int(acct['telegram_id'])):,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
