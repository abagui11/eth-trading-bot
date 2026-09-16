"""Does LIVE_MIN_FILL_RR admit the mill trades worth taking?

Motivation: 37.5% of mill cards fail the Accept-time R:R gate at their own mint
price, so they can never be filled by anyone. Before moving that threshold, the
question is whether the trades it excludes are ones we want.

The gate scores an idea on the **average of the targets still ahead**. That is
the right measure for a multi-contract HQ clip that scales out along a ladder.
It is the wrong measure for a mill clip, because a mill clip is exactly one
nano contract and `ladder.contract_rungs` closes a one-contract position
**fully at TP1** -- TP2 and TP3 are recorded but unreachable. So the gate is
judging these trades on an exit they never take.

This replays what actually happens:

  entry  the M5 close at `sent_at` -- the mill fills at market when the idea is
         minted, so this is the price it would really have got, not the
         published `entry`, which the fill never uses
  exit   TP1 in full, or the stop in full, whichever the bars reach first
  bars   M5, with M1 re-walk when one bar touches both; still tied = stop first
  edge   24h horizon, then marked out at the last close

Everything else is held constant across the sweep, so the only thing varying is
the threshold. Read-only: never writes to the ideas DB.

    python deploy/_study_rr_floor.py [days]
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config                    # noqa: E402
import config                        # noqa: E402,F401  (loads .env → IDEAS_DB)
import trade_ideas_bridge as bridge  # noqa: E402

CANDLES_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
MAX_BARS = 300
HORIZON_SEC = 24 * 3600
CACHE = "/tmp/_rr_floor_candles.db"


@dataclass(frozen=True)
class Bar:
    ts: int
    low: float
    high: float
    close: float


# --- candles ---------------------------------------------------------------

def _cache() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bars (product TEXT, gran INT, ts INT,"
        " low REAL, high REAL, close REAL, PRIMARY KEY (product, gran, ts))"
    )
    return conn


def fetch(product: str, start: int, end: int, gran: int) -> list[Bar]:
    conn = _cache()
    have = {
        r[0]: Bar(r[0], r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT ts, low, high, close FROM bars WHERE product=? AND gran=?"
            " AND ts BETWEEN ? AND ?", (product, gran, start, end),
        )
    }
    # One round trip per page of missing history; the cache makes the sweep
    # cheap to re-run, which is the point of sweeping at all.
    expected = set(range(start - start % gran, end + 1, gran))
    if len(have) < len(expected) * 0.9:
        cursor = start
        while cursor < end:
            stop = min(cursor + MAX_BARS * gran, end)
            try:
                resp = requests.get(
                    CANDLES_URL.format(product=product),
                    params={
                        "granularity": gran,
                        "start": datetime.fromtimestamp(cursor, timezone.utc).isoformat(),
                        "end": datetime.fromtimestamp(stop, timezone.utc).isoformat(),
                    },
                    timeout=20,
                )
                resp.raise_for_status()
                rows = resp.json()
            except Exception:
                rows = []
            with conn:
                for r in rows:
                    conn.execute(
                        "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?)",
                        (product, gran, int(r[0]), float(r[1]), float(r[2]),
                         float(r[4])),
                    )
                    have[int(r[0])] = Bar(int(r[0]), float(r[1]), float(r[2]),
                                          float(r[4]))
            cursor = stop
            time.sleep(0.12)
    conn.close()
    return [have[t] for t in sorted(have) if start <= t <= end]


# --- replay ----------------------------------------------------------------

def resolve(bars: list[Bar], subbars, *, long: bool, entry: float,
            stop: float, tp1: float) -> tuple[str, float]:
    """One contract: full exit at TP1 or full exit at the stop. Ties go to the
    stop, after an M1 re-walk fails to separate them."""
    for bar in bars:
        hit_tp = bar.high >= tp1 if long else bar.low <= tp1
        hit_sl = bar.low <= stop if long else bar.high >= stop
        if hit_tp and hit_sl:
            finer = subbars(bar.ts)
            if finer:
                outcome = resolve(finer, lambda _: [], long=long, entry=entry,
                                  stop=stop, tp1=tp1)
                if outcome[0] != "OPEN":
                    return outcome
            return "SL", stop
        if hit_tp:
            return "TP1", tp1
        if hit_sl:
            return "SL", stop
    return ("MARK", bars[-1].close) if bars else ("OPEN", entry)


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 45
    conn = bridge._connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ideas WHERE entry IS NOT NULL AND stop_loss IS NOT NULL"
        "  AND direction IN ('long','short')"
        "  AND COALESCE(sent_at, created_at) >= date('now', ?)"
        " ORDER BY id", (f"-{days} days",),
    ).fetchall()
    conn.close()
    print(f"loaded {len(rows)} mill ideas over {days} days\n")

    results = []
    for i, row in enumerate(rows):
        d = dict(row)
        product = str(d["product_id"])
        long = str(d["direction"]) == "long"
        stop_p = float(d["stop_loss"])
        tps = bridge._parse_take_profits(d["take_profits_json"])
        if not tps:
            continue
        ts = _ts(d.get("sent_at") or d.get("created_at"))
        if ts is None:
            continue

        bars = fetch(product, ts, ts + HORIZON_SEC, 300)
        if not bars:
            continue
        entry = bars[0].close  # market fill at mint

        risk = abs(entry - stop_p)
        if risk <= 0:
            continue
        # The gate's own measure, computed at the price the fill would use.
        ahead = [t for t in tps if (t > entry if long else t < entry)]
        if not ahead:
            continue
        rr_gate = abs(sum(ahead) / len(ahead) - entry) / risk
        tp1 = ahead[0]
        rr_tp1 = abs(tp1 - entry) / risk

        def subbars(bar_ts: int, _p=product):
            return fetch(_p, bar_ts, bar_ts + 300, 60)

        label, px = resolve(bars, subbars, long=long, entry=entry,
                            stop=stop_p, tp1=tp1)
        pnl = (px - entry) if long else (entry - px)
        results.append({
            "id": d["id"], "rr_gate": rr_gate, "rr_tp1": rr_tp1,
            "outcome": label, "r": pnl / risk,
            "family": str(d["signal_key"] or "?").split(":")[0],
        })
        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(rows)}")

    if not results:
        print("no replayable ideas")
        return 1

    print(f"\n=== {len(results)} replayable mill ideas ===")
    print("exit model: one contract, full out at TP1 or SL (ladder.contract_rungs)\n")

    _describe("ALL", results)

    print("\n--- sweep of LIVE_MIN_FILL_RR (gate's avg-of-targets measure) ---")
    print(f"{'floor':>6} {'admitted':>9} {'mean R':>8} {'total R':>9} {'win%':>7}")
    for floor in (0.0, 0.5, 0.6, 0.7, 0.79, 0.9, 1.0, 1.25, 1.5, 2.0, 2.5):
        keep = [r for r in results if r["rr_gate"] >= floor]
        if not keep:
            continue
        mean = sum(r["r"] for r in keep) / len(keep)
        wins = sum(1 for r in keep if r["r"] > 0)
        marker = "  <-- shipped" if abs(floor - bot_config.LIVE_MIN_FILL_RR) < 1e-9 else ""
        print(f"{floor:>6.2f} {len(keep):>9} {mean:>8.3f} "
              f"{sum(r['r'] for r in keep):>9.1f} "
              f"{wins / len(keep) * 100:>6.1f}%{marker}")

    print("\n--- the band the gate currently excludes ---")
    _describe("excluded (rr_gate < 1.0)",
              [r for r in results if r["rr_gate"] < bot_config.LIVE_MIN_FILL_RR])
    _describe("admitted (rr_gate >= 1.0)",
              [r for r in results if r["rr_gate"] >= bot_config.LIVE_MIN_FILL_RR])

    print("\n--- by signal family ---")
    fams: dict[str, list] = {}
    for r in results:
        fams.setdefault(r["family"], []).append(r)
    for fam, rs in sorted(fams.items(), key=lambda kv: -len(kv[1])):
        _describe(fam, rs)

    with open("/tmp/_rr_floor.json", "w") as fh:
        json.dump(results, fh)
    print("\nper-idea results: /tmp/_rr_floor.json")
    return 0


def _describe(label: str, rs: list) -> None:
    if not rs:
        print(f"  {label:<26} (none)")
        return
    mean = sum(r["r"] for r in rs) / len(rs)
    wins = sum(1 for r in rs if r["r"] > 0)
    tp1s = sum(1 for r in rs if r["outcome"] == "TP1")
    rr_tp1 = sum(r["rr_tp1"] for r in rs) / len(rs)
    # A fixed reward:risk needs a known hit rate to break even; printing both
    # is what says whether the geometry is survivable at all.
    breakeven = 1.0 / (1.0 + rr_tp1) * 100 if rr_tp1 else float("nan")
    print(f"  {label:<26} n={len(rs):<4} mean {mean:>+6.3f}R  "
          f"total {sum(r['r'] for r in rs):>+7.1f}R  win {wins / len(rs) * 100:>5.1f}%  "
          f"TP1 {tp1s / len(rs) * 100:>5.1f}%  avg TP1 {rr_tp1:.2f}R  "
          f"needs {breakeven:.1f}%")


def _ts(value) -> int | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


if __name__ == "__main__":
    raise SystemExit(main())
