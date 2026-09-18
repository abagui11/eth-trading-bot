"""Return one test account to brand-new, so onboarding can be recorded twice.

The first-contact states are one-shot: once an id is approved, `/start` shows
the welcome rather than the "request sent for review" message, and a wallet
cannot be re-registered as a first registration. That makes a demo take
unrepeatable, which is a bad reason to get a worse recording.

    python deploy/_reset_test_user.py <telegram_id> --yes

Refuses an account holding cash or an open stake unless --force, because
"reset the demo account" and "delete a tester's balance" are one typo apart.
Never touches any other id.

`/unsubscribe <telegram_id>` in Telegram does the same job with the same
guards and does not need a shell, so prefer it. This stays for the case where
the bot is down, and for `--force`, which has no in-band equivalent on
purpose.
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
#
# `pool_chain_deposits` is detached rather than deleted — see `reset`.
TABLES = pool._UNSUBSCRIBE_TABLES


def row_counts(uid: int) -> dict[str, int]:
    """Rows this id owns, per table. Absent tables are skipped, not fatal."""
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        for table in TABLES:
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE telegram_id = ?",
                    (uid,),
                ).fetchone()
            except sqlite3.OperationalError:
                continue    # table or column absent on this build
            if int(row["n"]):
                counts[table] = int(row["n"])
    finally:
        conn.close()
    return counts


def in_flight_payouts(uid: int) -> int:
    """Payouts that may already exist at the venue.

    Not covered by the cash guard: the money is debited at request time, so
    an account with a send in progress can read as empty. Deleting the row
    would destroy the only record that a payment might have happened, and
    Coinbase offers no idempotency and no way to ask. `--force` does not
    override this one.
    """
    conn = sqlite3.connect(config.LEDGER_DB)
    try:
        placeholders = ",".join("?" * len(pool._WITHDRAWALS_IN_FLIGHT))
        row = conn.execute(
            "SELECT COUNT(1) FROM pool_withdrawals WHERE telegram_id = ? "
            f"AND status IN ({placeholders})",
            (uid, *pool._WITHDRAWALS_IN_FLIGHT),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()
    return int(row[0] or 0)


def reset(uid: int, *, confirm: bool, force: bool) -> dict:
    """Decide and, if allowed, perform the reset. Returns what happened.

    Split out from the CLI so the refusal can be tested: it is the only thing
    standing between a demo reset and deleting a real tester's balance.

    `pool_chain_deposits` rows are detached instead of deleted. The deposit
    sweep re-inserts any transfer it cannot find, and past the first run a
    re-inserted row lands as `unmatched` — so deleting the row makes a
    historical deposit reappear as money that arrived with no owner and pages
    an admin about it. `baseline` keeps the "already seen" fact that makes
    crediting idempotent, without the link to the person.
    """
    account = pool.get_account(uid)
    cash = float(account["cash_usd"]) if account else 0.0
    reserved = float(account["reserved_usd"]) if account else 0.0
    counts = row_counts(uid)

    if not counts:
        return {"action": "nothing", "cash": cash, "reserved": reserved,
                "counts": counts}
    open_payouts = in_flight_payouts(uid)
    if open_payouts:
        return {"action": "refused_in_flight", "cash": cash,
                "reserved": reserved, "counts": counts,
                "in_flight": open_payouts}
    if (cash > 0.01 or reserved > 0.01) and not force:
        return {"action": "refused", "cash": cash, "reserved": reserved,
                "counts": counts}
    if not confirm:
        return {"action": "dry_run", "cash": cash, "reserved": reserved,
                "counts": counts}

    conn = sqlite3.connect(config.LEDGER_DB)
    try:
        with conn:
            conn.execute(
                "UPDATE pool_chain_deposits SET telegram_id = NULL, "
                "deposit_request_id = NULL, status = 'baseline', note = ? "
                "WHERE telegram_id = ?",
                (f"detached when {uid} was reset", uid),
            )
            for table in counts:
                conn.execute(f"DELETE FROM {table} WHERE telegram_id = ?", (uid,))
    finally:
        conn.close()
    return {"action": "reset", "cash": cash, "reserved": reserved,
            "counts": counts}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("telegram_id", type=int)
    ap.add_argument("--yes", action="store_true", help="actually delete")
    ap.add_argument("--force", action="store_true",
                    help="proceed even if the account holds money")
    args = ap.parse_args()
    uid = args.telegram_id

    account = pool.get_account(uid)
    print(f"telegram id : {uid}")
    print(f"account     : {'exists' if account else 'none'}")
    print(f"cash        : ${float(account['cash_usd']) if account else 0:,.2f}")
    print(f"reserved    : "
          f"${float(account['reserved_usd']) if account else 0:,.2f}")

    wallet = pool.get_wallet(uid)
    if wallet:
        print(f"wallet      : {wallet['address']} ({wallet['status']})")

    result = reset(uid, confirm=args.yes, force=args.force)
    counts = result["counts"]

    if result["action"] == "nothing":
        print("\nnothing to reset — this id is already brand-new.")
        return 0

    print("\nrows that would be deleted:" if result["action"] != "reset"
          else "\nrows deleted:")
    for table, n in counts.items():
        print(f"  {n:>4}  {table}")

    if result["action"] == "refused_in_flight":
        print(f"\nREFUSED: {result['in_flight']} payout(s) still in flight. "
              "That row is the only record a send may already have happened, "
              "so it must not be deleted. Wait for it to settle (/payouts).")
        return 1
    if result["action"] == "refused":
        held = result["cash"] + result["reserved"]
        print(f"\nREFUSED: this account still holds ${held:,.2f}. Withdraw it "
              "first, or pass --force if you are certain the money is not "
              "real.")
        return 1
    if result["action"] == "dry_run":
        print("\ndry run. re-run with --yes to delete.")
        return 0

    print(f"\nreset. {uid} is now a first-contact user again.")
    print(f"tester cash across the pool: ${pool.total_tester_cash():,.2f}")
    print("\n/start will show the 'request sent for review' message and ping "
          "you to Admit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
