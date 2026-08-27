#!/usr/bin/env python3
"""Drop live v2 paper trades that opened before August 2026.

The v2 book started mid-July after the $5k reset; the experiment we want to
measure starts 2026-08-01. July fills are deleted from the live book (not
moved into v1 archive). Cash is restated so topline metrics match the
trimmed log.

Example:
  sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \\
    /opt/eth-trading-agent/deploy/trim_paper_july.py --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import paper

CUTOFF = "2026-08-01T00:00:00Z"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete v2 paper trades opened before August 2026."
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show counts without writing.",
    )
    parser.add_argument("--cutoff", default=CUTOFF)
    args = parser.parse_args()

    paper.init_db()
    closed = paper.get_closed_trades(limit=500)
    would_drop = [
        t for t in closed if str(t.get("opened_at") or "") < args.cutoff
    ]
    opens = [
        p
        for p in paper.get_open_positions()
        if str(p.get("opened_at") or "") < args.cutoff
    ]
    realized = sum(float(t.get("realized_pnl_usd") or 0) for t in would_drop)

    print("Trim v2 paper trades opened before", args.cutoff)
    print(f"  Closed trades to drop: {len(would_drop)}")
    print(f"  Open positions to drop: {len(opens)}")
    print(f"  Realized P&L to reverse: ${realized:,.2f}")

    if args.dry_run:
        print("\nDry run — no changes made.")
        return 0
    if not args.yes:
        answer = input("\nDelete these live-book rows? Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return 1

    summary = paper.trim_trades_opened_before(args.cutoff)
    print("Done.")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
