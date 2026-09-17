"""Read-only: is the stance policy doing what we think, and did anything break?

Run this after enabling STANCE_PUBLISH_DETERMINISTIC, and periodically during
the measurement window. Opens the ledger read-only and writes nothing.

What it answers
---------------
1. Is the board still producing a full batch every cycle? (bug tripwire)
2. What fraction of rows is the policy actually changing, and of what kind?
3. Is the Phase 2 shadow read populating, and how often does it withhold bias?

What it deliberately does NOT claim
-----------------------------------
Whether the policy *helped*. The board-keyed mill lanes ran n=240 over five
weeks, so a two-week window holds ~90 trades — far too few to detect a change
of the size at issue. This is a check for breakage (batches stopped, override
rate went to 0% or 100%, reads all stale), not a test of the hypothesis.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

# Windows consoles default to cp1252 and this prints arrows and dashes.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot_config  # noqa: E402
import config  # noqa: E402

EPOCH = getattr(bot_config, "STANCE_POLICY_EPOCH", None)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.LEDGER_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def main() -> int:
    print(f"ledger        : {config.LEDGER_DB}")
    print(f"policy epoch  : {EPOCH}")
    print(f"publish det   : {getattr(bot_config, 'STANCE_PUBLISH_DETERMINISTIC', None)}")
    print(f"require evid  : {getattr(bot_config, 'STANCE_OVERRIDE_REQUIRE_EVIDENCE', None)}")
    print(f"cond. reads   : {getattr(bot_config, 'INTEL_CONDITIONAL_READS_ENABLED', None)} "
          f"{getattr(bot_config, 'INTEL_CONDITIONAL_TIMEFRAMES', ())}")
    problems: list[str] = []

    with _conn() as conn:
        cols = _cols(conn, "intel_stances")
        if "llm_stance" not in cols:
            print("\n!! intel_stances has no llm_stance column — migration has "
                  "not run on this book (it runs on the next init_db).")
            problems.append("migration pending")

        print("\n=== board health: rows per cycle, last 12 cycles ===")
        rows = conn.execute(
            "SELECT cycle_ts, COUNT(*) n, COUNT(DISTINCT product_id||timeframe) uniq "
            "FROM intel_stances GROUP BY cycle_ts ORDER BY cycle_ts DESC LIMIT 12"
        ).fetchall()
        for r in rows:
            flag = "" if r["uniq"] == 6 else "   <-- incomplete batch"
            print(f"  {r['cycle_ts']}  rows={r['n']:3d} unique={r['uniq']}{flag}")
        if rows and any(r["uniq"] != 6 for r in rows[:3]):
            problems.append("recent batch incomplete")
        if not rows:
            problems.append("no stance rows at all")

        if "llm_stance" in cols and EPOCH:
            for label, where, params in (
                ("BEFORE epoch", "created_at < ?", (EPOCH,)),
                ("SINCE epoch", "created_at >= ?", (EPOCH,)),
            ):
                print(f"\n=== {label} ===")
                total = conn.execute(
                    f"SELECT COUNT(*) c FROM intel_stances WHERE {where}", params
                ).fetchone()["c"]
                if not total:
                    print("  (no rows)")
                    continue
                published_differs = conn.execute(
                    f"SELECT COUNT(*) c FROM intel_stances WHERE {where} "
                    "AND llm_stance IS NOT NULL AND stance != llm_stance", params
                ).fetchone()["c"]
                kinds = Counter({
                    r["override_kind"] or "(kept)": r["c"]
                    for r in conn.execute(
                        f"SELECT override_kind, COUNT(*) c FROM intel_stances "
                        f"WHERE {where} GROUP BY override_kind", params)
                })
                print(f"  rows={total:,}")
                print(f"  published != model's stance: {published_differs:,} "
                      f"({published_differs / total * 100:.1f}%)  "
                      "<- what the policy is changing")
                for kind, n in kinds.most_common():
                    print(f"    model attempted {kind:20s} {n:6,} "
                          f"({n / total * 100:.1f}%)")
                if label == "SINCE epoch":
                    attempted = total - kinds.get("(kept)", 0)
                    rate = attempted / total if total else 0
                    # ~10% attempted overrides is the recorded norm; 0% or
                    # >50% means the prompt or the parse broke, not that the
                    # model changed its mind.
                    if total >= 60 and not 0.01 <= rate <= 0.50:
                        problems.append(
                            f"override attempt rate {rate:.0%} is outside the "
                            "1-50% band seen historically")

        print("\n=== Phase 2 shadow reads ===")
        if "intel_reads" not in {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }:
            print("  table absent — created on the next init_db()")
        else:
            total = conn.execute("SELECT COUNT(*) c FROM intel_reads").fetchone()["c"]
            print(f"  rows={total:,}")
            if total:
                for r in conn.execute(
                    "SELECT source, bias, repelling_state, COUNT(*) c "
                    "FROM intel_reads GROUP BY 1,2,3 ORDER BY c DESC LIMIT 12"
                ):
                    print(f"    source={r['source']:12s} bias={str(r['bias']):8s} "
                          f"state={str(r['repelling_state']):14s} {r['c']:6,}")
                stale = conn.execute(
                    "SELECT COUNT(*) c FROM intel_reads WHERE stale_invalidation = 1"
                ).fetchone()["c"]
                withheld = conn.execute(
                    "SELECT COUNT(*) c FROM intel_reads WHERE bias IS NULL"
                ).fetchone()["c"]
                print(f"  bias withheld: {withheld:,} ({withheld / total * 100:.1f}%)"
                      "  <- null is expected, not a failure")
                print(f"  stale invalidation: {stale:,} "
                      f"({stale / total * 100:.1f}%)  <- target < 5%")
                if stale / total > 0.05:
                    problems.append("stale-invalidation rate above 5%")

    print("\n" + "=" * 64)
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        print("\nThese are breakage checks. Passing them does not mean the "
              "policy helped — that needs the Phase 2 scorer and more time.")
        return 1
    print("No breakage detected. Note this says nothing about whether the "
          "policy helped: the mill's board-keyed lanes are far too small to "
          "resolve that inside a two-week window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
