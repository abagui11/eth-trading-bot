"""Persist signal state between hourly cycles (24h range announcements)."""

from __future__ import annotations

import json
import sqlite3

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    return conn


def init_state() -> None:
    with _connect() as conn:
        conn.execute(_SCHEMA)
        conn.commit()


def get_state(key: str) -> dict | None:
    init_state()
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM signal_state WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def set_state(key: str, value: dict) -> None:
    init_state()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO signal_state (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
        conn.commit()
