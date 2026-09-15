"""Read-only-ish: walk the wallet + deposit flow on a throwaway id, then undo.

Exercises the real ledger.db so the deployed schema and indexes are what gets
tested, then deletes every row it made. Refuses to touch an id that already
has an account, so it can never be pointed at a real tester.

    sudo -u ethagent .venv/bin/python deploy/_verify_wallet_flow.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import pool  # noqa: E402

TEST_A = -90001
TEST_B = -90002
W1 = "0x" + "aa" * 20
W2 = "0x" + "bb" * 20
ADMIN = pool.admin_ids()[0] if pool.admin_ids() else 1
TX = "0x" + "cd" * 32


def cleanup() -> None:
    conn = sqlite3.connect(config.LEDGER_DB)
    with conn:
        for table in ("pool_wallets", "pool_deposit_requests", "pool_events",
                      "pool_intents", "pool_accounts", "approved_users"):
            conn.execute(
                f"DELETE FROM {table} WHERE telegram_id IN (?, ?)",
                (TEST_A, TEST_B),
            )
    conn.close()


def main() -> int:
    for uid in (TEST_A, TEST_B):
        if pool.get_account(uid) is not None:
            print(f"refusing to run: {uid} already has an account")
            return 2

    ok = True

    def check(label: str, got, want) -> None:
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok ' if good else 'FAIL'}  {label}: {got!r}")

    try:
        pool.approve_user(TEST_A, admin_id=ADMIN)
        pool.approve_user(TEST_B, admin_id=ADMIN)

        print("deposit before a wallet is registered")
        check("refused", pool.request_deposit(TEST_A, 600.0, txid=TX).get("reason"),
              "wallet_required")

        print("register, and the address starts unproven")
        check("registered", pool.register_wallet(TEST_A, W1).get("ok"), True)
        check("status", pool.get_wallet(TEST_A)["status"], "pending")
        check("payout refused", pool.payout_target(TEST_A).get("reason"), "unverified")

        print("a second tester cannot claim the same address")
        check("refused", pool.register_wallet(TEST_B, W1).get("reason"), "address_taken")

        print("deposit credited, sender matches -> wallet proven")
        req = pool.request_deposit(TEST_A, 600.0, txid=TX)
        check("filed", req.get("ok"), True)
        decided = pool.decide_deposit(
            req["request_id"], admin_id=ADMIN, approve=True, sender=W1
        )
        check("credited", decided.get("status"), "credited")
        check("verified", decided.get("wallet_verified"), True)
        check("payout allowed", pool.payout_target(TEST_A).get("address"), W1)

        print("changing the address is not self-serve")
        check("refused", pool.register_wallet(TEST_A, W2).get("reason"),
              "change_needs_admin")
        chg = pool.request_wallet_change(TEST_A, W2)
        check("change filed", chg.get("ok"), True)
        check("old address still live", pool.get_wallet(TEST_A)["address"], W1)

        print("approved change swaps it, unproven again, payouts held")
        pool.decide_wallet_change(chg["request_id"], admin_id=ADMIN, approve=True)
        check("new address", pool.get_wallet(TEST_A)["address"], W2)
        check("payout held", pool.payout_target(TEST_A).get("reason"), "unverified")
        pool.mark_wallet_verified(W2, txid=TX)
        check("held even once proven",
              pool.payout_target(TEST_A).get("reason"), "cooldown")
    finally:
        cleanup()
        print("\ncleaned up; accounts gone:",
              pool.get_account(TEST_A) is None and pool.get_account(TEST_B) is None)

    print("\nALL CHECKS PASSED" if ok else "\nSOMETHING FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
