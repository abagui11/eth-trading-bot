#!/usr/bin/env python3
"""Archive mill volume paper opened before MILL_PAPER_EPOCH_START.

The daily digest and /volume mill book then only see the restarted epoch.
Personal user_paper_trades are not touched.

Example:
  sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \\
    /opt/eth-trading-agent/deploy/reset_mill_paper_epoch.py --dry-run

  sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \\
    /opt/eth-trading-agent/deploy/reset_mill_paper_epoch.py --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot_config
import config  # noqa: F401  — load .env so IDEAS_DB is set
import trade_ideas_bridge


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive mill volume paper from before the digest epoch."
    )
    parser.add_argument(
        "--since",
        default=bot_config.MILL_PAPER_EPOCH_START,
        help="Keep rows with opened_at >= this UTC date (default: config).",
    )
    parser.add_argument("--yes", action="store_true", help="Apply the archive.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows that would move; do not write.",
    )
    args = parser.parse_args()
    since = str(args.since or "").strip()
    if not since:
        print("MILL_PAPER_EPOCH_START is empty", file=sys.stderr)
        return 2
    if not trade_ideas_bridge.enabled():
        print("IDEAS_DB is missing", file=sys.stderr)
        return 1

    current = trade_ideas_bridge.mill_paper_trades_since("0000")
    moving = [t for t in current if str(t.get("opened_at") or "") < since]
    keeping = [t for t in current if str(t.get("opened_at") or "") >= since]
    print(f"  Cutoff:     opened_at >= {since}")
    print(f"  Would move: {len(moving)}")
    print(f"  Would keep: {len(keeping)}")
    if args.dry_run or not args.yes:
        if not args.yes:
            print("Pass --yes to archive (or --dry-run to only count).")
        return 0

    summary = trade_ideas_bridge.archive_mill_paper_before(since)
    print(
        f"  Archived {summary['archived']}; "
        f"{summary['remaining']} remain in the live mill book."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
