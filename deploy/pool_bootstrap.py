"""Grandfather the existing beta list into the tester pool as unfunded accounts.

Turning on ``POOL_ENABLED`` makes the product approval-gated: ``access.is_allowed``
stops letting users in just because the paywall is off. Anyone already talking to
the bot would be locked out and would have to be re-Admitted one card at a time.

This admits them up front with a zero-balance pool account, so access is
unchanged and the only new thing they see is the deposit nudge. Money is never
moved here — a balance only ever arrives through an admin Credit.

    python deploy/pool_bootstrap.py --dry-run
    python deploy/pool_bootstrap.py --apply

Idempotent: ``pool.approve_user`` upserts, so re-running is a no-op.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import access  # noqa: E402
import config  # noqa: E402
import pool  # noqa: E402


def candidates() -> list[dict]:
    """Everyone who had access before the pool flag: env allowlist + subscribers.

    The allowlist is often empty in deployments that run with the paywall off,
    in which case the subscribers table *is* the access list.
    """
    seen: dict[int, dict] = {}
    for row in access.list_subscribers():
        uid = int(row["telegram_id"])
        seen[uid] = {"telegram_id": uid, "username": row.get("username"), "source": "subscriber"}
    for uid in sorted(access.load_allowed_ids()):
        seen.setdefault(int(uid), {"telegram_id": int(uid), "username": None, "source": "allowlist"})
    return [seen[k] for k in sorted(seen)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="print what would change")
    group.add_argument("--apply", action="store_true", help="write the approvals")
    parser.add_argument(
        "--admin-id",
        type=int,
        default=None,
        help="admin id recorded as the approver (defaults to the first pool admin)",
    )
    args = parser.parse_args()

    admins = pool.admin_ids()
    if not admins:
        print(
            "ERROR: no pool admins resolved. Set POOL_ADMIN_TELEGRAM_IDS in .env "
            "before enabling the pool, or nobody can Admit anyone.",
            file=sys.stderr,
        )
        return 2
    admin_id = args.admin_id if args.admin_id is not None else admins[0]

    rows = candidates()
    print(f"ledger: {config.LEDGER_DB}")
    print(f"pool admins: {admins}")
    print(f"{len(rows)} candidate(s) from the pre-pool access list\n")

    changed = 0
    for row in rows:
        uid = row["telegram_id"]
        status = pool.access_status(uid)
        account = pool.get_account(uid)
        cash = float(account["cash_usd"]) if account else 0.0
        if status == "approved" and account is not None:
            print(f"  = {uid:<12} {row['username'] or '':<12} already approved, ${cash:,.2f}")
            continue
        label = "approve" if args.apply else "would approve"
        print(f"  + {uid:<12} {row['username'] or '':<12} {label} ({row['source']}, unfunded)")
        if args.apply:
            pool.approve_user(uid, admin_id=admin_id, username=row["username"])
        changed += 1

    print(f"\n{changed} account(s) {'created' if args.apply else 'pending'}.")
    if not args.apply:
        print("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
