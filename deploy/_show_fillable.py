"""Which mill ideas would fill right now, for a given Telegram id.

Read-only: runs the real gates in dry-run mode and places nothing.

    python deploy/_show_fillable.py 8708390551
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config  # noqa: E402
import config  # noqa: E402
import pool  # noqa: E402
import trade_ideas_bridge as bridge  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    uid = int(sys.argv[1])

    import execute

    cap = execute.mill_capacity()
    print(f"user {uid}  approved={pool.is_approved(uid)} "
          f"funded={pool.is_funded(uid)}  may_fill={bridge.may_fill(uid)}")
    print(f"LIVE_MILL_ANY_ACCEPT_FILLS={bot_config.LIVE_MILL_ANY_ACCEPT_FILLS}  "
          f"operators={bot_config.LIVE_MILL_FILL_TELEGRAM_IDS}")
    print(f"LIVE_MIN_FILL_RR={bot_config.LIVE_MIN_FILL_RR}  "
          f"EXECUTION_MODE={config.EXECUTION_MODE}")
    print(f"sleeve: open={cap['open']} slots_free={cap['slots_free']} "
          f"halted={cap['halted']}\n")

    rows = bridge.fillable_ideas(uid, limit=25)
    print(f"{'id':>5}  {'product':<9} {'dir':<6} {'status':<9} "
          f"{'fills':<6} reason")
    for row in rows:
        v = row["preview"]
        reason = "" if row["would_fill"] else str(v.get("skip_reason") or "?")
        born = v.get("born_rr")
        extra = f"  (R:R at mint {born:.2f})" if born is not None else ""
        print(f"{row['id']:>5}  {str(row['product_id']):<9} "
              f"{str(row['direction']):<6} {str(row['status']):<9} "
              f"{str(row['would_fill']):<6} {reason}{extra}")

    ok = [r for r in rows if r["would_fill"]]
    print(f"\n{len(ok)} of {len(rows)} would fill right now")
    if ok:
        best = ok[0]
        print(f"newest fillable: #{best['id']} {best['product_id']} "
              f"{best['direction']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
