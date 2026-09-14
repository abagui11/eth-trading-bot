"""Append-only SQLite ledger for trade suggestions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config
from models import Suggestion

# TODO: split into paper vs actual ledgers for the full build.

# Why a cycle ended the way it did. `executed = 0` conflates all of the
# abstaining cases below, so counting them apart used to mean LIKE-matching
# prose. Mirrors the role `skip_reason` plays in `vault_allocations`.
REASON_CODES = (
    "trade",  # actionable idea survived to broadcast
    "model_no_trade",  # the model read the chart and declined
    "validation_rejected",  # validate.py refused the levels (R/R, stop distance, OB)
    "proposal_error",  # API or JSON failure — no verdict was reached
    "proposal_empty",  # call returned no usable decision for any product
    "not_evaluated",  # backfilled so both products keep a row; not a judgement
    "audit_downgrade",  # critic or hard audit block killed an actionable idea
    "watchdog_shadow",  # watchdog trigger logged but execution disabled
)


def classify_reason(suggestion: Suggestion) -> str:
    """Best-effort reason code for a suggestion that did not set one itself.

    Explicit `reason_code` always wins; this only covers callers that predate
    the field, and it reads the prose prefixes `analyze.py` already writes.
    """
    explicit = getattr(suggestion, "reason_code", None)
    if explicit:
        return str(explicit)
    if suggestion.action != "no_trade":
        return "trade"
    rationale = suggestion.rationale or ""
    if "parse_error:" in rationale:
        return "validation_rejected"
    if "api_error:" in rationale:
        return "proposal_error"
    if rationale.startswith("Audit downgrade"):
        return "audit_downgrade"
    return "model_no_trade"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    action TEXT NOT NULL,
    size REAL,
    entry REAL,
    stop_loss REAL,
    take_profits TEXT,
    risk_reward REAL,
    price_at_suggestion REAL,
    rationale TEXT,
    chart_path TEXT,
    setup_tags TEXT
);
"""


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add new columns to existing ledgers without migration framework."""
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(suggestions)").fetchall()
    }
    if "setup_tags" not in cols:
        conn.execute("ALTER TABLE suggestions ADD COLUMN setup_tags TEXT")
    if "product_id" not in cols:
        conn.execute(
            "ALTER TABLE suggestions ADD COLUMN product_id TEXT NOT NULL DEFAULT 'ETH-USD'"
        )
    if "executed" not in cols:
        conn.execute(
            "ALTER TABLE suggestions ADD COLUMN executed INTEGER NOT NULL DEFAULT 1"
        )
    if "trigger_name" not in cols:
        conn.execute("ALTER TABLE suggestions ADD COLUMN trigger_name TEXT")
    if "macro_json" not in cols:
        conn.execute("ALTER TABLE suggestions ADD COLUMN macro_json TEXT")
    if "reason_code" not in cols:
        conn.execute("ALTER TABLE suggestions ADD COLUMN reason_code TEXT")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_SCHEMA)
        _ensure_columns(conn)
        conn.commit()


def append(
    suggestion: Suggestion,
    cycle_id: str,
    price_at_suggestion: float,
    chart_path: str,
    ts: str | None = None,
    setup_tags: str | None = None,
    *,
    executed: bool = True,
    trigger_name: str | None = None,
    macro_json: str | dict[str, Any] | None = None,
    reason_code: str | None = None,
) -> int:
    """Append one suggestion row. Returns the new row id."""
    init_db()
    row_ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    product_id = getattr(suggestion, "product_id", None) or "ETH-USD"
    trigger = trigger_name or getattr(suggestion, "trigger_name", None)
    reason = reason_code or classify_reason(suggestion)
    if isinstance(macro_json, dict):
        macro_payload = json.dumps(macro_json)
    else:
        macro_payload = macro_json

    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO suggestions (
                ts, cycle_id, action, size, entry, stop_loss,
                take_profits, risk_reward, price_at_suggestion, rationale, chart_path,
                setup_tags, product_id, executed, trigger_name, macro_json,
                reason_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_ts,
                cycle_id,
                suggestion.action,
                suggestion.size,
                suggestion.entry,
                suggestion.stop_loss,
                json.dumps(suggestion.take_profits),
                suggestion.risk_reward,
                price_at_suggestion,
                suggestion.rationale,
                chart_path,
                setup_tags,
                product_id,
                1 if executed else 0,
                trigger,
                macro_payload,
                reason,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def get_latest_suggestion() -> dict | None:
    """Return the most recent ledger row, or None if empty."""
    rows = get_latest(1)
    return rows[0] if rows else None


def get_latest_trade_suggestion() -> dict | None:
    """Return the most recent non-no_trade suggestion, or None."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM suggestions
            WHERE action != 'no_trade'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return _row_to_record(row)


def get_latest_for_product(product_id: str, *, traded_only: bool = False) -> dict | None:
    """Most recent suggestion for one product, or None.

    The eva_day variant gates its deterministic M1 triggers on this so it only
    ever trades in the direction Eva's last vision read supports — the whole
    point of the gate is that the fast bot never forms its own directional
    opinion, it only times an existing one.
    """
    init_db()
    sql = "SELECT * FROM suggestions WHERE product_id = ?"
    if traded_only:
        sql += " AND action != 'no_trade'"
    sql += " ORDER BY id DESC LIMIT 1"
    with _connect() as conn:
        row = conn.execute(sql, (product_id,)).fetchone()
    return _row_to_record(row) if row is not None else None


def get_suggestion_by_cycle_id(cycle_id: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM suggestions WHERE cycle_id = ? ORDER BY id DESC LIMIT 1",
            (cycle_id,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_record(row)


def require_cycle_recorded(cycle_id: str) -> dict:
    """Return the ledger row for cycle_id or raise if the cycle was not persisted."""
    row = get_suggestion_by_cycle_id(cycle_id)
    if row is None:
        raise RuntimeError(f"Ledger row missing for cycle_id={cycle_id!r}")
    return row


def get_latest(n: int = 10) -> list[dict]:
    """Return the most recent n ledger rows as plain dicts."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM suggestions
            ORDER BY id DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()

    return [_row_to_record(row) for row in rows]


def search_rationale(query: str, limit: int = 5) -> list[dict]:
    """Find suggestions whose rationale contains query (case-insensitive)."""
    query = query.strip()
    if not query:
        return []
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM suggestions
            WHERE rationale LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (f"%{query}%", limit),
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def format_history_summary(rows: list[dict], *, max_rationale_chars: int = 220) -> str:
    """Compact multi-cycle summary for chat context."""
    if not rows:
        return ""
    lines = ["Hourly trade update history (newest first):"]
    for row in rows:
        action = str(row.get("action") or "n/a")
        ts = row.get("ts") or ""
        cycle_id = row.get("cycle_id") or ""
        rationale = str(row.get("rationale") or "").strip().replace("\n", " ")
        if len(rationale) > max_rationale_chars:
            rationale = rationale[:max_rationale_chars].rstrip() + "..."
        chart = row.get("chart_path") or "n/a"
        lines.append(
            f"- {ts} | cycle {cycle_id} | {action} | charts: {chart}\n  rationale: {rationale}"
        )
    return "\n".join(lines)


def _row_to_record(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["take_profits"] = json.loads(record["take_profits"] or "[]")
    if "executed" in record and record["executed"] is not None:
        record["executed"] = bool(record["executed"])
    raw_macro = record.get("macro_json")
    if isinstance(raw_macro, str) and raw_macro.strip():
        try:
            record["macro"] = json.loads(raw_macro)
        except json.JSONDecodeError:
            record["macro"] = None
    else:
        record["macro"] = None
    return record


if __name__ == "__main__":
    fake = Suggestion(
        action="spot_buy",
        size=0.5,
        entry=2400.0,
        stop_loss=2350.0,
        take_profits=[2500.0, 2600.0, 2700.0],
        risk_reward=2.0,
        rationale="Ledger checkpoint — fake suggestion",
    )
    cycle_id = "test_cycle_001"
    row_id = append(
        fake,
        cycle_id=cycle_id,
        price_at_suggestion=2410.5,
        chart_path="charts/test_H1_annotated.png",
    )
    print(f"Appended row id={row_id}")

    latest = get_latest_suggestion()
    assert latest is not None
    print(json.dumps(latest, indent=2))

    assert latest["cycle_id"] == cycle_id
    assert latest["action"] == "spot_buy"
    assert latest["take_profits"] == [2500.0, 2600.0, 2700.0]
    assert latest["price_at_suggestion"] == 2410.5
    print("Checkpoint passed.")
