"""Append-only SQLite ledger for trade suggestions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import config
from models import Suggestion

# TODO: split into paper vs actual ledgers for the full build.

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
) -> int:
    """Append one suggestion row. Returns the new row id."""
    init_db()
    row_ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO suggestions (
                ts, cycle_id, action, size, entry, stop_loss,
                take_profits, risk_reward, price_at_suggestion, rationale, chart_path,
                setup_tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    record = dict(row)
    record["take_profits"] = json.loads(record["take_profits"] or "[]")
    return record


def get_suggestion_by_cycle_id(cycle_id: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM suggestions WHERE cycle_id = ? ORDER BY id DESC LIMIT 1",
            (cycle_id,),
        ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["take_profits"] = json.loads(record["take_profits"] or "[]")
    return record


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

    results = []
    for row in rows:
        record = dict(row)
        record["take_profits"] = json.loads(record["take_profits"] or "[]")
        results.append(record)
    return results


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
