"""Why was this idea refused? Replays the Accept-time gate and shows the math.

    python deploy/_diag_idea.py 1027
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config          # noqa: E402
import research           # noqa: E402
import trade_ideas_bridge as bridge  # noqa: E402


def main() -> int:
    idea_id = int(sys.argv[1])
    conn = bridge._connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    conn.close()
    if row is None:
        print(f"no idea #{idea_id}")
        return 1
    d = dict(row)

    print(f"=== idea #{idea_id} ===")
    for k in ("product_id", "direction", "entry", "stop_loss",
              "take_profits_json", "confidence", "status", "signal_key",
              "created_at", "sent_at", "live_fill_type"):
        print(f"  {k:<18} {d.get(k)}")

    entry = float(d["entry"])
    stop = float(d["stop_loss"])
    tps = bridge._parse_take_profits(d["take_profits_json"])
    long = str(d["direction"]) == "long"

    spot = research.get_spot_price(product_id=str(d["product_id"]))
    print(f"\n  live mark          {spot:,.2f}")

    risk = abs(entry - stop)
    print(f"  planned risk (1R)  {risk:,.2f}  ({risk / entry * 100:.2f}%)")

    # Chase: how far the mark has run past the entry, in R.
    drift = (spot - entry) if long else (entry - spot)
    print(f"\n--- chase gate (LIVE_MAX_CHASE_R = {bot_config.LIVE_MAX_CHASE_R}) ---")
    print(f"  drift past entry   {drift:,.2f}  = {drift / risk:.2f}R")
    print(f"  {'REFUSED (chasing)' if drift / risk > bot_config.LIVE_MAX_CHASE_R else 'ok'}")

    # R:R from the live mark, against targets still ahead.
    ahead = [t for t in tps if (t > spot if long else t < spot)]
    new_risk = abs(spot - stop)
    print(f"\n--- R:R gate (LIVE_MIN_FILL_RR = {bot_config.LIVE_MIN_FILL_RR}) ---")
    print(f"  targets            {tps}")
    print(f"  still ahead        {ahead}")
    print(f"  risk from mark     {new_risk:,.2f}")
    if ahead:
        avg = sum(ahead) / len(ahead)
        reward = abs(avg - spot)
        rr = reward / new_risk if new_risk else 0.0
        print(f"  avg target ahead   {avg:,.2f}  reward {reward:,.2f}")
        print(f"  R:R                {rr:.2f}")
        print(f"  {'REFUSED (risks more than it makes)' if rr < bot_config.LIVE_MIN_FILL_RR else 'ok'}")
    else:
        print("  REFUSED (no targets left ahead of the mark)")

    print("\n--- what the card was born with ---")
    orig_ahead = [t for t in tps if (t > entry if long else t < entry)]
    if orig_ahead:
        avg0 = sum(orig_ahead) / len(orig_ahead)
        print(f"  R:R at mint        {abs(avg0 - entry) / risk:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
