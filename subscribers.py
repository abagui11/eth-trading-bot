"""List Telegram users who messaged the bot — for manual allowlist onboarding."""

from __future__ import annotations

import access
import config


def _fmt_user(row: dict) -> str:
    username = row.get("username")
    handle = f"@{username}" if username else "(no username)"
    status = "ALLOWED" if row["telegram_id"] in access.load_allowed_ids() else "PENDING"
    return (
        f"  {row['telegram_id']:>12}  {handle:<20}  {status:<8}  last_seen={row['last_seen']}"
    )


def main() -> None:
    access.init_db()
    allowed_ids = config.ALLOWED_TELEGRAM_IDS
    all_subs = access.list_subscribers()
    pending = access.pending_subscribers()
    active = access.active_subscribers()

    print("=== ETH Trading Agent — Subscribers ===\n")
    print(f"Allowlist (.env):  {','.join(str(i) for i in allowed_ids) or '(empty)'}")
    print(f"Registered total:  {len(all_subs)}")
    print(f"Active (allowed):  {len(active)}")
    print(f"Pending approval:  {len(pending)}\n")

    if all_subs:
        print("All users who messaged the bot:")
        print(f"  {'telegram_id':>12}  {'username':<20}  {'status':<8}  last_seen")
        for row in all_subs:
            print(_fmt_user(row))
    else:
        print("No one has messaged the bot yet.")
        print("Share the bot link — they must send /start once (paywall is OK).")

    if pending:
        print("\n--- Pending (add these IDs to ALLOWED_TELEGRAM_IDS) ---")
        for row in pending:
            handle = f"@{row['username']}" if row.get("username") else str(row["telegram_id"])
            print(f"  {row['telegram_id']}  ({handle})")

        new_ids = list(allowed_ids) + [row["telegram_id"] for row in pending]
        # Only show IDs that aren't already allowed (user may only want to add some)
        print("\nCopy into .env (add only the IDs you want to approve):")
        print(f"ALLOWED_TELEGRAM_IDS={','.join(str(i) for i in allowed_ids)}")
        if pending:
            add_example = ",".join(str(i) for i in allowed_ids + [pending[0]["telegram_id"]])
            print(f"# e.g. after approving first pending user:")
            print(f"# ALLOWED_TELEGRAM_IDS={add_example}")

    print("\n--- After editing .env ---")
    print("  Local:  restart main.py")
    print("  Cloud:  sudo systemctl restart eth-agent")
    print("\nSQLite:  SELECT telegram_id, username, active FROM subscribers;")


if __name__ == "__main__":
    main()
