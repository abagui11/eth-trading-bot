"""SQLite persistence for macro headlines and pulse advisories."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash TEXT NOT NULL,
    source TEXT,
    title TEXT NOT NULL,
    url TEXT,
    summary TEXT,
    published_at TEXT,
    ingested_at TEXT NOT NULL,
    keyword_score INTEGER NOT NULL DEFAULT 0,
    keyword_hits TEXT,
    severity INTEGER NOT NULL DEFAULT 0,
    eth_bias TEXT,
    category TEXT,
    eth_impact_summary TEXT,
    posture_hints TEXT,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'ignored',
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_macro_events_url_hash ON macro_events(url_hash);
CREATE INDEX IF NOT EXISTS idx_macro_events_ingested ON macro_events(ingested_at);
CREATE INDEX IF NOT EXISTS idx_macro_events_status ON macro_events(status);

CREATE TABLE IF NOT EXISTS macro_pulses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    open_positions_json TEXT,
    advisory_json TEXT,
    text_summary TEXT,
    FOREIGN KEY (event_id) REFERENCES macro_events(id)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()


def url_hash(url: str | None, title: str) -> str:
    key = (url or "").strip().lower() or title.strip().lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def has_recent_url_hash(url_hash_value: str, *, days: int = 7) -> bool:
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM macro_events WHERE url_hash = ? AND ingested_at >= ? LIMIT 1",
            (url_hash_value, cutoff),
        ).fetchone()
    return row is not None


def insert_event(
    *,
    source: str | None,
    title: str,
    url: str | None,
    summary: str | None,
    published_at: str | None,
    keyword_score: int,
    keyword_hits: list[dict],
    severity: int = 0,
    eth_bias: str | None = None,
    category: str | None = None,
    eth_impact_summary: str | None = None,
    posture_hints: list[str] | None = None,
    expires_at: str | None = None,
    status: str = "ignored",
    raw_json: dict | None = None,
) -> dict[str, Any]:
    init_db()
    ingested_at = _now_iso()
    hash_value = url_hash(url, title)
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO macro_events (
                url_hash, source, title, url, summary, published_at, ingested_at,
                keyword_score, keyword_hits, severity, eth_bias, category,
                eth_impact_summary, posture_hints, expires_at, status, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hash_value,
                source,
                title,
                url,
                summary,
                published_at,
                ingested_at,
                keyword_score,
                json.dumps(keyword_hits),
                severity,
                eth_bias,
                category,
                eth_impact_summary,
                json.dumps(posture_hints or []),
                expires_at,
                status,
                json.dumps(raw_json or {}),
            ),
        )
        conn.commit()
        event_id = int(cur.lastrowid)
    return get_event(event_id) or {}


def get_event(event_id: int) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM macro_events WHERE id = ?", (event_id,)
        ).fetchone()
    return _row_to_event(row) if row else None


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("keyword_hits", "posture_hints", "raw_json"):
        raw = data.get(key)
        if raw:
            try:
                data[key] = json.loads(str(raw))
            except json.JSONDecodeError:
                data[key] = raw
        else:
            data[key] = [] if key != "raw_json" else {}
    return data


def list_events(
    *,
    limit: int = 50,
    status: str | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    init_db()
    now = _now_iso()
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if active_only:
        clauses.append("status = 'classified'")
        clauses.append("severity >= 3")
        clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(now)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM macro_events {where} ORDER BY ingested_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def get_active_events(*, min_severity: int = 3) -> list[dict[str, Any]]:
    init_db()
    now = _now_iso()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM macro_events
            WHERE status = 'classified'
              AND severity >= ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY severity DESC, ingested_at DESC
            """,
            (min_severity, now),
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def insert_pulse(
    *,
    event_id: int,
    open_positions: list[dict],
    advisory: dict,
    text_summary: str,
) -> dict[str, Any]:
    init_db()
    ts = _now_iso()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO macro_pulses (event_id, ts, open_positions_json, advisory_json, text_summary)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_id,
                ts,
                json.dumps(open_positions),
                json.dumps(advisory),
                text_summary,
            ),
        )
        conn.commit()
        pulse_id = int(cur.lastrowid)
    return get_latest_pulse_for_event(event_id) or {"id": pulse_id, "ts": ts}


def get_latest_pulse_for_event(event_id: int) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM macro_pulses WHERE event_id = ?
            ORDER BY ts DESC LIMIT 1
            """,
            (event_id,),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    for key in ("open_positions_json", "advisory_json"):
        if data.get(key):
            data[key] = json.loads(str(data[key]))
    return data


def get_monitored_feed_labels() -> list[str]:
    """Human-readable list of configured RSS sources for dashboard."""
    from config import MACRO_FEED_URLS

    labels: list[str] = []
    for url in MACRO_FEED_URLS:
        host = url.split("/")[2] if "://" in url else url
        labels.append(host.replace("www.", ""))
    labels.append("Webhook ingest")
    labels.append("Telegram /macro")
    return labels


def prune_old_events(*, days: int = 7) -> int:
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM macro_events WHERE ingested_at < ? AND severity = 0",
            (cutoff,),
        )
        conn.commit()
        return int(cur.rowcount)
