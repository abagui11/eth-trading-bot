"""Who is on the pool, and what is open right now.

Scratch helper for picking the right telegram id for a demo and seeing which
live trades a demo card could mirror.
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def rows(conn, sql):
    try:
        return [dict(r) for r in conn.execute(sql)]
    except sqlite3.Error as exc:
        print(f"  ({exc})")
        return []


def main() -> int:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row

    print("=== pool accounts ===")
    for r in rows(conn, "select telegram_id, username, status, cash_usd,"
                        " reserved_usd from pool_accounts order by telegram_id"):
        print(f"  {r['telegram_id']}  @{r['username'] or '-':<18} "
              f"{r['status']:<10} cash ${float(r['cash_usd']):,.2f} "
              f"reserved ${float(r['reserved_usd']):,.2f}")

    print("\n=== subscribers (can be DM'd) ===")
    for r in rows(conn, "select * from subscribers"):
        print(f"  {r.get('telegram_id')}  @{r.get('username') or '-'}")

    print("\n=== open live trades ===")
    for r in rows(conn, "select id, source, cycle_id, product_id, side, entry,"
                        " stop_loss, take_profits_json, qty, qty_open, status,"
                        " opened_at from live_trades where status='open'"
                        " order by id desc limit 20"):
        print(f"  #{r['id']} {r['source']:<5} {r['product_id']} "
              f"{r['side']:<5} entry {r['entry']} stop {r['stop_loss']} "
              f"tps {r['take_profits_json']} qty {r['qty_open'] or r['qty']} "
              f"opened {r['opened_at']}")

    print("\n=== live pending (waiting on entry) ===")
    for r in rows(conn, "select * from live_pending order by rowid desc limit 20"):
        print(f"  {r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
