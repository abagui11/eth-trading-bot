"""Read-only: what the deposit watcher has seen, and what it did about it."""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import pool  # noqa: E402

print("baselined at:", pool.get_meta("chain_deposits_baselined") or "(never run)")

conn = sqlite3.connect(config.LEDGER_DB)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT * FROM pool_chain_deposits ORDER BY first_seen_at"
).fetchall()
print(f"\n{len(rows)} transfer(s) on the deposit address:\n")
for r in rows:
    print(
        f"  ${float(r['amount_usd']):>10,.2f}  {r['status']:<10} "
        f"user={r['telegram_id']}  {r['cb_tx_id']}"
    )
    print(f"              hash {r['txid']}")
    if r["note"]:
        print(f"              note: {r['note']}")

print(f"\nunmatched awaiting /assign: {len(pool.unmatched_chain_deposits())}")
print(f"tester cash total:          ${pool.total_tester_cash():,.2f}")
print(f"claimed but uncredited:     ${pool.pending_inbound_usd():,.2f}")

dep = conn.execute(
    "SELECT COUNT(*) AS n FROM pool_events WHERE kind = 'deposit' AND "
    "ref LIKE 'cb_deposit:%'"
).fetchone()
print(f"auto-credit events booked:  {dep['n']}")
conn.close()
