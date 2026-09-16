"""Remove a legacy demo paper book for an account that is now live.

The demo book (`user_books`) predates the pool. An account holding real money
should not also carry an imaginary one -- it shows up in /me and reads like a
second balance.

Touches `user_*` tables only. It can never affect `pool_*` or `live_trades`,
so real money is out of reach by construction.

    python deploy/_drop_demo_book.py 8708390551            # show
    python deploy/_drop_demo_book.py 8708390551 --delete   # do it
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

TABLES = (
    ("user_accounts", "telegram_id"),
    ("user_positions", "telegram_id"),
    ("user_trades", "telegram_id"),
    ("trade_decisions", "telegram_id"),
)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    uid = int(sys.argv[1])
    delete = "--delete" in sys.argv

    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row

    account = conn.execute(
        "SELECT * FROM user_accounts WHERE telegram_id = ?", (uid,)
    ).fetchone()
    if account is None:
        print(f"{uid}: no demo book")
        return 0

    print(f"{uid}: demo book of ${float(account['starting_usd']):,.2f} "
          f"(cash ${float(account['cash_usd']):,.2f})")
    counts = {}
    for table, col in TABLES:
        try:
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (uid,)
            ).fetchone()[0]
        except sqlite3.Error as exc:
            counts[table] = f"({exc})"
    for table, n in counts.items():
        print(f"  {table:<18} {n}")

    # An open demo position is not money, but deleting it silently would hide
    # that the book was mid-trade. Say so.
    try:
        open_n = conn.execute(
            "SELECT COUNT(*) FROM user_positions WHERE telegram_id = ?"
            " AND status = 'open'", (uid,)
        ).fetchone()[0]
        if open_n:
            print(f"  NOTE: {open_n} open demo position(s) will be discarded")
    except sqlite3.Error:
        pass

    if not delete:
        print("\ndry run — pass --delete to remove")
        return 0

    with conn:
        for table, col in TABLES:
            try:
                conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (uid,))
            except sqlite3.Error as exc:
                print(f"  skip {table}: {exc}")
    print("\ndeleted. real balances untouched:")
    for row in conn.execute(
        "SELECT telegram_id, cash_usd, reserved_usd FROM pool_accounts"
        " WHERE telegram_id = ?", (uid,)
    ):
        print(f"  pool cash ${float(row['cash_usd']):,.2f} "
              f"reserved ${float(row['reserved_usd']):,.2f}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
