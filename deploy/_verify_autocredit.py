"""Walk the auto-credit path on the deployed ledger with a throwaway id, then undo.

The Coinbase read is already proven by deploy/_probe_deposits.py; what this
exercises is everything after it, against the real schema and indexes -- the
hash match, the venue-amount rule, idempotence across repeated sweeps, and the
refusal to apportion an unclaimed transfer.

Refuses to run against an id that already has an account, and deletes every
row it creates, including the synthetic transfers.

    sudo -u ethagent .venv/bin/python deploy/_verify_autocredit.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import pool  # noqa: E402

UID = -90101
WALLET = "0x" + "ee" * 20
HASH_OK = "0x" + "a1" * 32
HASH_ORPHAN = "0x" + "b2" * 32
CB_OK = "verify-cb-ok"
CB_ORPHAN = "verify-cb-orphan"
ADMIN = pool.admin_ids()[0] if pool.admin_ids() else 1


def transfer(cb_id: str, amount: float, txid: str) -> dict:
    return {"id": cb_id, "amount": amount, "currency": "USDC", "txid": txid,
            "network": "ethereum", "created_at": ""}


def cleanup() -> None:
    conn = sqlite3.connect(config.LEDGER_DB)
    with conn:
        for table in ("pool_wallets", "pool_deposit_requests", "pool_events",
                      "pool_intents", "pool_accounts", "approved_users"):
            conn.execute(f"DELETE FROM {table} WHERE telegram_id = ?", (UID,))
        conn.execute(
            "DELETE FROM pool_chain_deposits WHERE cb_tx_id IN (?, ?)",
            (CB_OK, CB_ORPHAN),
        )
    conn.close()


def main() -> int:
    if pool.get_account(UID) is not None:
        print(f"refusing to run: {UID} already has an account")
        return 2

    ok = True

    def check(label: str, got, want) -> None:
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok ' if good else 'FAIL'}  {label}: {got!r}")

    try:
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)
        req = pool.request_deposit(UID, 1000.0, txid=HASH_OK)
        check("claim filed", req.get("ok"), True)

        print("transfer arrives for 900 against a 1000 claim")
        events = pool.observe_chain_deposits([transfer(CB_OK, 900.0, HASH_OK)])
        check("credited", [e["kind"] for e in events], ["credited"])
        check("venue amount wins", float(pool.get_account(UID)["cash_usd"]), 900.0)
        check("mismatch flagged", events[0]["mismatch"], True)
        check("claim closed",
              pool.get_deposit_request(req["request_id"])["status"], "credited")

        print("the sweep re-reads the same row every 60s")
        for _ in range(4):
            pool.observe_chain_deposits([transfer(CB_OK, 900.0, HASH_OK)])
        check("still paid once", float(pool.get_account(UID)["cash_usd"]), 900.0)

        print("admin card cannot pay it again")
        check("refused",
              pool.decide_deposit(req["request_id"], admin_id=ADMIN,
                                  approve=True).get("ok"), False)
        check("balance unchanged", float(pool.get_account(UID)["cash_usd"]), 900.0)

        print("an unclaimed transfer is flagged, not apportioned")
        before = pool.total_tester_cash()
        events = pool.observe_chain_deposits(
            [transfer(CB_ORPHAN, 5000.0, HASH_ORPHAN)]
        )
        check("unmatched", [e["kind"] for e in events], ["unmatched"])
        check("nobody credited", pool.total_tester_cash(), before)

        print("arrival does not verify the wallet")
        check("still unproven", pool.get_wallet(UID)["status"], "pending")
        check("payout refused", pool.payout_target(UID).get("reason"), "unverified")
    finally:
        cleanup()
        print("\ncleaned up; account gone:", pool.get_account(UID) is None)
        print("tester cash back to: $%.2f" % pool.total_tester_cash())

    print("\nALL CHECKS PASSED" if ok else "\nSOMETHING FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
