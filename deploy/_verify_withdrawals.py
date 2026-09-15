"""Walk the withdrawal flow on the deployed ledger with a throwaway id, undo it.

Exercises the states where money is actually lost -- debit-on-request, refund
on rejection, the fee true-up, and the `unknown` outcome that must neither
refund nor retry -- against the real schema rather than a test double.

Never touches the network: no real payout is sent.

    sudo -u ethagent .venv/bin/python deploy/_verify_withdrawals.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config  # noqa: E402
import config  # noqa: E402
import pool  # noqa: E402

UID = -90202
WALLET = "0x" + "ab" * 20
ADMIN = pool.admin_ids()[0] if pool.admin_ids() else 1


def cleanup() -> None:
    conn = sqlite3.connect(config.LEDGER_DB)
    with conn:
        for table in ("pool_withdrawals", "pool_wallets", "pool_deposit_requests",
                      "pool_events", "pool_intents", "pool_accounts",
                      "approved_users"):
            conn.execute(f"DELETE FROM {table} WHERE telegram_id = ?", (UID,))
    conn.close()
    if pool.payouts_halted():
        pool.resume_payouts()


def main() -> int:
    if pool.get_account(UID) is not None:
        print(f"refusing: {UID} already exists")
        return 2
    was_halted = pool.payouts_halted()
    if was_halted:
        print(f"NOTE: payouts are currently halted ({was_halted}); "
              "will restore that at the end")

    ok = True

    def check(label: str, got, want) -> None:
        nonlocal ok
        good = got == want if not isinstance(want, float) else abs(got - want) < 0.01
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {label}: {got!r}")

    try:
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref=f"verify:{UID}")

        print("an unverified wallet cannot be paid")
        check("refused", pool.request_withdrawal(UID, 100.0).get("reason"),
              "unverified")

        pool.mark_wallet_verified(WALLET)
        reserve = float(bot_config.POOL_WITHDRAWAL_FEE_RESERVE_USD)

        print("\nthe debit happens at request time")
        req = pool.request_withdrawal(UID, 500.0)
        check("queued", req.get("ok"), True)
        wid = int(req["withdrawal_id"])
        check("cash after hold", float(pool.get_account(UID)["cash_usd"]),
              500.0 - reserve)

        print("\na second request cannot spend the same balance")
        check("refused", pool.request_withdrawal(UID, 500.0).get("reason"),
              "insufficient_available")

        print("\nrejection refunds in full")
        pool.decide_withdrawal(wid, admin_id=ADMIN, approve=False)
        check("cash restored", float(pool.get_account(UID)["cash_usd"]), 1000.0)

        print("\nthe fee reserve is trued up to the real fee")
        req2 = pool.request_withdrawal(UID, 500.0)
        wid2 = int(req2["withdrawal_id"])
        pool.decide_withdrawal(wid2, admin_id=ADMIN, approve=True)
        check("claimed for sending", pool.mark_withdrawal_submitting(wid2), True)
        check("cannot be claimed twice",
              pool.mark_withdrawal_submitting(wid2), False)
        pool.mark_withdrawal_submitted(wid2, cb_tx_id="verify-cb",
                                       fee_usd=0.148356)
        check("cash after real fee", float(pool.get_account(UID)["cash_usd"]),
              1000.0 - 500.148356)
        check("debited recorded",
              float(pool.get_withdrawal(wid2)["debited_usd"]), 500.15)

        print("\nan unknown outcome neither refunds nor retries")
        req3 = pool.request_withdrawal(UID, 100.0)
        wid3 = int(req3["withdrawal_id"])
        before = float(pool.get_account(UID)["cash_usd"])
        pool.decide_withdrawal(wid3, admin_id=ADMIN, approve=True)
        pool.mark_withdrawal_submitting(wid3)
        pool.mark_withdrawal_unknown(wid3, reason="verify: simulated timeout")
        check("not refunded", float(pool.get_account(UID)["cash_usd"]), before)
        check("status", pool.get_withdrawal(wid3)["status"], "unknown")
        check("queue halted", bool(pool.payouts_halted()), True)
        check("nothing left to send", pool.pending_withdrawals("approved"), [])
    finally:
        cleanup()
        print("\ncleaned up; account gone:", pool.get_account(UID) is None)
        print("payouts halted:", pool.payouts_halted())
        print("tester cash total: $%.2f" % pool.total_tester_cash())

    print("\nALL CHECKS PASSED" if ok else "\nSOMETHING FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
