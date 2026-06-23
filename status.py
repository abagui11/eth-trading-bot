"""Quick pre-flight check before starting scheduler.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import config
import ledger


def main() -> None:
    ledger.init_db()
    rows = ledger.get_latest(5)
    charts = sorted(
        config.CHARTS_DIR.glob("*_H1_annotated.png"),
        key=lambda p: p.stat().st_mtime,
    )

    api_ok = len(config.ANTHROPIC_API_KEY) > 20 and "your" not in config.ANTHROPIC_API_KEY.lower()
    tg_ok = ":" in config.TELEGRAM_BOT_TOKEN and "test" not in config.TELEGRAM_BOT_TOKEN.lower()

    print("=== Pre-scheduler check ===\n")
    print(f"ANTHROPIC_MODEL:     {config.ANTHROPIC_MODEL}")
    print(f"Anthropic API key:   {'OK (set)' if api_ok else 'CHECK .env — may be invalid'}")
    print(f"Telegram bot token:  {'OK (set)' if tg_ok else 'CHECK .env — may be invalid'}")
    print(f"Telegram chat ID:    {config.TELEGRAM_CHAT_ID}")
    print(f"PORTFOLIO_VALUE:     {config.PORTFOLIO_VALUE}")
    print(f"Ledger row count:    {len(ledger.get_latest(100))}")
    print(f"Annotated charts:    {len(charts)}")

    if rows:
        latest = rows[0]
        chart_path = Path(latest["chart_path"])
        print(f"\nLatest ledger row (id={latest['id']}):")
        print(f"  cycle_id:  {latest['cycle_id']}")
        print(f"  action:    {latest['action']}")
        print(f"  price:     {latest['price_at_suggestion']}")
        print(f"  chart:     {chart_path.name}")
        print(f"  exists:    {chart_path.exists()}")

    if charts:
        print(f"\nNewest chart file: {charts[-1]}")

    print("\n=== Recent ledger (last 3) ===")
    print(json.dumps(rows[:3], indent=2))

    if not api_ok or not tg_ok:
        print("\nFix .env before scheduler.py — see .env.example")
        sys.exit(1)

    print("\nReady. Run: python agent.py  (once)  or  python scheduler.py  (hourly)")


if __name__ == "__main__":
    main()
