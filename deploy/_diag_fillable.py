"""How many mill cards could ever have been filled?

Applies the Accept-time gate to each idea *at its own mint price*, which is the
most favourable moment it will ever see. An idea that fails here was never
fillable by anyone -- the card was broadcast, Accept was tapped, and the refusal
blamed market drift that had not happened.

    python deploy/_diag_fillable.py [days]
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config                    # noqa: E402
import config                        # noqa: E402,F401  (loads .env → IDEAS_DB)
import trade_ideas_bridge as bridge  # noqa: E402


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    conn = bridge._connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ideas WHERE entry IS NOT NULL AND stop_loss IS NOT NULL"
        "  AND direction IN ('long','short')"
        "  AND COALESCE(sent_at, created_at) >= date('now', ?)"
        " ORDER BY id",
        (f"-{days} days",),
    ).fetchall()
    conn.close()

    floor = float(bot_config.LIVE_MIN_FILL_RR)
    verdicts: Counter[str] = Counter()
    rrs: list[float] = []
    by_signal: dict[str, list[int]] = {}

    for row in rows:
        d = dict(row)
        entry, stop = float(d["entry"]), float(d["stop_loss"])
        tps = bridge._parse_take_profits(d["take_profits_json"])
        long = str(d["direction"]) == "long"
        risk = abs(entry - stop)
        if risk <= 0 or not tps:
            verdicts["bad_levels"] += 1
            continue
        ahead = [t for t in tps if (t > entry if long else t < entry)]
        if not ahead:
            verdicts["no_targets_ahead"] += 1
            continue
        rr = abs(sum(ahead) / len(ahead) - entry) / risk
        rrs.append(rr)
        ok = rr >= floor
        verdicts["fillable_at_mint" if ok else "DEAD_ON_ARRIVAL"] += 1

        family = str(d["signal_key"] or "?").split(":")[0]
        by_signal.setdefault(family, []).append(1 if ok else 0)

    total = sum(verdicts.values())
    print(f"=== {total} mill ideas in the last {days} day(s) ===")
    print(f"gate: avg-of-targets R:R >= LIVE_MIN_FILL_RR = {floor}\n")
    for k, v in verdicts.most_common():
        print(f"  {k:<20} {v:>4}  ({v / total * 100:5.1f}%)")

    if rrs:
        rrs.sort()
        def pct(p: float) -> float:
            return rrs[min(len(rrs) - 1, int(len(rrs) * p))]
        print(f"\n  R:R at mint — median {pct(0.5):.2f}, "
              f"p25 {pct(0.25):.2f}, p75 {pct(0.75):.2f}, max {rrs[-1]:.2f}")
        print(f"  share at or above {floor}: "
              f"{sum(1 for r in rrs if r >= floor) / len(rrs) * 100:.1f}%")

    print("\n--- by signal family (fillable at mint) ---")
    for fam, oks in sorted(by_signal.items(), key=lambda kv: -len(kv[1])):
        print(f"  {fam:<24} {sum(oks):>3}/{len(oks):<3} "
              f"({sum(oks) / len(oks) * 100:5.1f}%)")

    print("\n--- what actually filled ---")
    conn = bridge._connect()
    conn.row_factory = sqlite3.Row
    filled = conn.execute(
        "SELECT live_fill_type, COUNT(*) n FROM ideas"
        " WHERE COALESCE(sent_at, created_at) >= date('now', ?)"
        " GROUP BY live_fill_type", (f"-{days} days",),
    ).fetchall()
    conn.close()
    for r in filled:
        print(f"  {str(r['live_fill_type'] or 'never filled'):<16} {r['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
