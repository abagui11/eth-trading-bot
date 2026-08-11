"""SQLite persistence for intelligence artifacts: stances, funding, long thesis."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intel_stances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts TEXT NOT NULL,
    product_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    stance TEXT NOT NULL,
    confidence REAL,
    rationale TEXT,
    source TEXT NOT NULL DEFAULT 'llm',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_stances_cycle ON intel_stances(cycle_ts);
CREATE INDEX IF NOT EXISTS idx_intel_stances_product ON intel_stances(product_id, timeframe);

CREATE TABLE IF NOT EXISTS intel_medium (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts TEXT NOT NULL,
    summary TEXT NOT NULL,
    btc_eth_note TEXT,
    funding_note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_medium_cycle ON intel_medium(cycle_ts);

CREATE TABLE IF NOT EXISTS funding_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    funding_ts TEXT NOT NULL,
    rate REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (product_id, funding_ts)
);

CREATE INDEX IF NOT EXISTS idx_funding_rates_product ON funding_rates(product_id, funding_ts);

CREATE TABLE IF NOT EXISTS funding_regimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    regime TEXT NOT NULL,
    streak_periods INTEGER NOT NULL DEFAULT 0,
    as_of_ts TEXT NOT NULL,
    detail_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_funding_regimes_product ON funding_regimes(product_id, created_at);

CREATE TABLE IF NOT EXISTS funding_health (
    product_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    source TEXT,
    last_ok_at TEXT,
    last_ok_funding_ts TEXT,
    last_error TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intel_long_thesis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of_date TEXT NOT NULL,
    cycle_phase TEXT NOT NULL,
    thesis_json TEXT NOT NULL,
    chart_path TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_long_thesis_date ON intel_long_thesis(as_of_date);

CREATE TABLE IF NOT EXISTS zmove_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    z REAL NOT NULL,
    bar_ts TEXT NOT NULL,
    detail_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_zmove_events_created ON zmove_events(created_at);
"""

VALID_STANCES = ("bullish", "neutral", "bearish")
STANCE_TIMEFRAMES = ("H4", "H1", "M15")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Stances

def insert_stances(
    cycle_ts: str,
    stances: list[dict[str, Any]],
    *,
    source: str = "llm",
) -> int:
    """Persist one hourly batch of per-product/per-timeframe stances."""
    init_db()
    created = _now_iso()
    count = 0
    with _connect() as conn:
        for s in stances:
            stance = str(s.get("stance") or "neutral").lower()
            if stance not in VALID_STANCES:
                stance = "neutral"
            conn.execute(
                """
                INSERT INTO intel_stances
                    (cycle_ts, product_id, timeframe, stance, confidence,
                     rationale, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_ts,
                    str(s["product_id"]),
                    str(s["timeframe"]).upper(),
                    stance,
                    float(s["confidence"]) if s.get("confidence") is not None else None,
                    str(s.get("rationale") or ""),
                    source,
                    created,
                ),
            )
            count += 1
        conn.commit()
    return count


def latest_stances() -> list[dict[str, Any]]:
    """Most recent stance batch (one row per product/timeframe for latest cycle_ts).

    ``cycle_ts`` is bucketed to the hour, so a restart or a manual re-run inside
    the same hour appends a second batch under the same key. Collapse those to
    the newest row per product/timeframe so callers never see duplicates.
    """
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT cycle_ts FROM intel_stances ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return []
        cycle_ts = str(row["cycle_ts"])
        rows = conn.execute(
            """
            SELECT * FROM intel_stances WHERE id IN (
                SELECT MAX(id) FROM intel_stances
                WHERE cycle_ts = ?
                GROUP BY product_id, timeframe
            )
            ORDER BY product_id, timeframe
            """,
            (cycle_ts,),
        ).fetchall()
    return [dict(r) for r in rows]


def stance_history(*, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM intel_stances
            ORDER BY created_at DESC, product_id, timeframe
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Medium summary

def insert_medium_summary(
    cycle_ts: str,
    summary: str,
    *,
    btc_eth_note: str | None = None,
    funding_note: str | None = None,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO intel_medium
                (cycle_ts, summary, btc_eth_note, funding_note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cycle_ts, summary, btc_eth_note, funding_note, _now_iso()),
        )
        conn.commit()


def latest_medium_summary() -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM intel_medium ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Funding

def upsert_funding_rates(
    product_id: str,
    rates: list[dict[str, Any]],
) -> int:
    """Insert funding prints, skipping duplicates. rates: [{ts, rate}]."""
    init_db()
    fetched = _now_iso()
    inserted = 0
    with _connect() as conn:
        for r in rates:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO funding_rates
                    (product_id, funding_ts, rate, fetched_at)
                VALUES (?, ?, ?, ?)
                """,
                (product_id, str(r["ts"]), float(r["rate"]), fetched),
            )
            inserted += int(cur.rowcount or 0)
        conn.commit()
    return inserted


def funding_series(product_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Newest-last funding prints for a product."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT funding_ts AS ts, rate FROM funding_rates
            WHERE product_id = ?
            ORDER BY funding_ts DESC LIMIT ?
            """,
            (product_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def insert_funding_regime(
    product_id: str,
    regime: str,
    *,
    streak_periods: int,
    as_of_ts: str,
    detail: dict[str, Any] | None = None,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO funding_regimes
                (product_id, regime, streak_periods, as_of_ts, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                regime,
                streak_periods,
                as_of_ts,
                json.dumps(detail or {}),
                _now_iso(),
            ),
        )
        conn.commit()


def latest_funding_regime(product_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM funding_regimes WHERE product_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (product_id,),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    if data.get("detail_json"):
        try:
            data["detail"] = json.loads(str(data["detail_json"]))
        except json.JSONDecodeError:
            data["detail"] = {}
    data.pop("detail_json", None)
    return data


def record_funding_health(
    product_id: str,
    *,
    status: str,
    source: str | None = None,
    funding_ts: str | None = None,
    error: str | None = None,
) -> None:
    """Record the outcome of a funding fetch. 'ok' refreshes the success marks;
    'error' keeps the previous success marks so staleness stays measurable."""
    init_db()
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO funding_health
                (product_id, status, source, last_ok_at, last_ok_funding_ts,
                 last_error, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                status = excluded.status,
                source = COALESCE(excluded.source, funding_health.source),
                last_ok_at = COALESCE(excluded.last_ok_at, funding_health.last_ok_at),
                last_ok_funding_ts = COALESCE(
                    excluded.last_ok_funding_ts, funding_health.last_ok_funding_ts
                ),
                last_error = excluded.last_error,
                checked_at = excluded.checked_at
            """,
            (
                product_id,
                status,
                source,
                now if status == "ok" else None,
                funding_ts if status == "ok" else None,
                error,
                now,
            ),
        )
        conn.commit()


def funding_health(product_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM funding_health WHERE product_id = ?",
            (product_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def funding_regime_history(
    product_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM funding_regimes WHERE product_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (product_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        data = dict(r)
        data.pop("detail_json", None)
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Long thesis

def insert_long_thesis(
    as_of_date: str,
    cycle_phase: str,
    thesis: dict[str, Any],
    *,
    chart_path: str | None = None,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO intel_long_thesis
                (as_of_date, cycle_phase, thesis_json, chart_path, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (as_of_date, cycle_phase, json.dumps(thesis), chart_path, _now_iso()),
        )
        conn.commit()


def latest_long_thesis() -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM intel_long_thesis ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    try:
        data["thesis"] = json.loads(str(data.pop("thesis_json")))
    except json.JSONDecodeError:
        data["thesis"] = {}
    return data


# ---------------------------------------------------------------------------
# Z-move events (persisted signal feed for API consumers)

def insert_zmove_event(
    product_id: str,
    metric: str,
    z: float,
    bar_ts: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO zmove_events
                (product_id, metric, z, bar_ts, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (product_id, metric, z, bar_ts, json.dumps(detail or {}), _now_iso()),
        )
        conn.commit()


def recent_zmove_events(*, limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM zmove_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        data = dict(r)
        if data.get("detail_json"):
            try:
                data["detail"] = json.loads(str(data["detail_json"]))
            except json.JSONDecodeError:
                data["detail"] = {}
        data.pop("detail_json", None)
        out.append(data)
    return out
