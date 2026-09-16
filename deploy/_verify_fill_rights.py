"""Who can take a live mill clip right now, and what the sleeve would allow."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config                    # noqa: E402
import config                        # noqa: E402,F401
import pool                          # noqa: E402
import trade_ideas_bridge as bridge  # noqa: E402


def main() -> int:
    print(f"LIVE_MILL_ANY_ACCEPT_FILLS = {bot_config.LIVE_MILL_ANY_ACCEPT_FILLS}")
    print(f"operators                  = {bot_config.LIVE_MILL_FILL_TELEGRAM_IDS}")
    print(f"LIVE_MIN_FILL_RR           = {bot_config.LIVE_MIN_FILL_RR}\n")

    print(f"{'telegram id':>12}  {'cash':>10}  {'funded':>6}  {'may fill':>8}")
    for account in pool.list_accounts():
        uid = int(account["telegram_id"])
        print(f"{uid:>12}  ${float(account['cash_usd']):>9,.2f}  "
              f"{str(pool.is_funded(uid)):>6}  {str(bridge.may_fill(uid)):>8}")

    import execute
    cap = execute.mill_capacity()
    print(f"\nmill sleeve: {cap.get('open')}/{cap.get('max_open')} open"
          f"{'  HALTED: ' + str(cap.get('halted')) if cap.get('halted') else ''}")
    for trade in cap.get("open_trades") or []:
        print(f"  #{trade.get('id')} {trade.get('product_id')} "
              f"{trade.get('side')} @ {trade.get('entry')} ({trade.get('fill_type')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
