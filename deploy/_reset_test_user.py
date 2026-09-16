"""Return one test account to brand-new, so onboarding can be recorded twice.

The first-contact states are one-shot: once an id is approved, `/start` shows
the welcome rather than the "request sent for review" message, and a wallet
cannot be re-registered as a first registration. That makes a demo take
unrepeatable, which is a bad reason to get a worse recording.

    python deploy/_reset_test_user.py <telegram_id> --yes

Refuses an account holding cash or an open stake unless --force, because
"reset the demo account" and "delete a tester's balance" are one typo apart.
Never touches any other id.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import pool    # noqa: E402

# Every table keyed by telegram_id that onboarding writes to. Ordered
# child-first so nothing is orphaned midway if one statement fails.
TABLES = (
    "pool_withdrawals",
    "pool_wallet_checks",
    "pool_wallets",
    "pool_deposit_requests",
    "pool_stakes",
    "pool_intents",
    "pool_events",
    "pool_chain_deposits",
    "pool_accounts",
    "approved_users",
    "subscribers",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("telegram_id", type=int)
    ap.add_argument("--yes", action="store_true", help="actually delete")
    ap.add_argument("--force", action="store_true",
                    help="proceed even if the account holds money")
    args = ap.parse_args()
    uid = args.telegram_id

    account = pool.get_account(uid)
    cash = float(account["cash_usd"]) if account else 0.0
    reserved = float(account["reserved_usd"]) if account else 0.0

    print(f"telegram id : {uid}")
    print(f"account     : {'exists' if account else 'none'}")
    print(f"cash        : ${cash:,.2f}")
    print(f"reserved    : ${reserved:,.2f}")

    wallet = pool.get_wallet(uid)
    if wallet:
        print(f"wallet      : {wallet['address']} ({wallet['status']})")

    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    counts = {}
    for table in TABLES:
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE telegram_id = ?", (uid,)
            ).fetchone()
            if int(row["n"]):
                counts[table] = int(row["n"])
        except sqlite3.OperationalError:
            continue        # table or column absent on this build

    if not counts:
        conn.close()
        print("\nnothing to reset — this id is already brand-new.")
        return 0

    print("\nrows that would be deleted:")
    for table, n in counts.items():
        print(f"  {n:>4}  {table}")

    if (cash > 0.01 or reserved > 0.01) and not args.force:
        conn.close()
        print(f"\nREFUSED: this account still holds ${cash + reserved:,.2f}. "
              "Withdraw it first, or pass --force if you are certain the money "
              "is not real.")
        return 1

    if not args.yes:
        conn.close()
        print("\ndry run. re-run with --yes to delete.")
        return 0

    with conn:
        for table in counts:
            conn.execute(f"DELETE FROM {table} WHERE telegram_id = ?", (uid,))
    conn.close()

    print(f"\nreset. {uid} is now a first-contact user again.")
    print(f"tester cash across the pool: ${pool.total_tester_cash():,.2f}")
    print("\n/start will show the 'request sent for review' message and ping "
          "you to Admit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
