#!/usr/bin/env python3
"""One-shot: migrate legacy Fund contributions into personal demo accounts.

Safe to re-run (idempotent). Does not alter house book cash.

Usage (from repo root, with venv):
  python deploy/migrate_personal_accounts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import paper
import user_books


def main() -> None:
    paper.init_db()
    result = user_books.migrate_funders_to_personal_accounts()
    print(
        f"Migrated {result['migrated']} funders to "
        f"${result['amount_usd']:,.0f} personal accounts "
        f"(skipped {result['skipped']} already present)."
    )


if __name__ == "__main__":
    main()
