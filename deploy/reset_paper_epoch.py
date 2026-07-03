#!/usr/bin/env python3
"""Archive the current paper epoch and reset to PAPER_PORTFOLIO_VALUE from .env.

Run once on the server after updating .env to PORTFOLIO_VALUE=5000 and
PAPER_PORTFOLIO_VALUE=5000.

Example:
  sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \\
    /opt/eth-trading-agent/deploy/reset_paper_epoch.py --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot_config
import config
import paper


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive paper trades/positions and start a new $5k epoch."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be archived without writing.",
    )
    parser.add_argument(
        "--prior-label",
        default="legacy_1k",
        help="Label for the epoch being archived (default: legacy_1k).",
    )
    args = parser.parse_args()

    paper.init_db()
    state = paper.get_state()
    epoch = paper.get_epoch_info()

    import sqlite3

    conn = sqlite3.connect(config.LEDGER_DB)
    trade_count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    conn.close()
    pos_count = len(paper.get_open_positions())
    starting = float(config.PAPER_PORTFOLIO_VALUE)

    print("Paper epoch reset")
    print(f"  DB: {config.LEDGER_DB}")
    print(f"  Current starting_usd: ${float(state.get('starting_usd') or 0):,.2f}")
    print(f"  Current cash_usd:     ${float(state.get('cash_usd') or 0):,.2f}")
    print(f"  Current epoch label:  {epoch.get('epoch_label')}")
    print(f"  Open positions:       {pos_count}")
    print(f"  paper_trades rows:    {trade_count}")
    print(f"  New starting_usd:     ${starting:,.2f}")
    print(f"  New epoch label:      {bot_config.PAPER_EPOCH_LABEL}")
    print(f"  Archive prior as:     {args.prior_label}")
    print(f"  ETH size bounds:      {bot_config.MIN_ETH_QTY} – {bot_config.MAX_ETH_QTY} ETH")

    if args.dry_run:
        print("\nDry run — no changes made.")
        return 0

    if not args.yes:
        answer = input("\nArchive all paper data and reset? Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return 1

    summary = paper.archive_epoch_and_reset(
        starting_usd=starting,
        epoch_label=bot_config.PAPER_EPOCH_LABEL,
        prior_epoch_label=args.prior_label,
    )
    print("\nDone.")
    print(f"  Archived {summary['archived_trade_rows']} trade rows")
    print(f"  Archived {summary['archived_position_rows']} position rows")
    print(f"  Prior epoch: {summary['prior_epoch_label']} (${summary['prior_starting_usd']:,.0f})")
    print(f"  New epoch:   {summary['new_epoch_label']} (${summary['new_starting_usd']:,.0f})")
    print(f"  Started at:  {summary['archived_at']}")
    print("\nRestart services if the agent is running:")
    print("  sudo systemctl restart eth-agent eth-dashboard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
