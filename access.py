"""Subscriber access control — manual allowlist now, billing hook later."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import config

# TODO: Stripe webhook / recurring subscription → activate subscriber in DB.

_SUBSCRIBERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_SUBSCRIBERS_SCHEMA)
        conn.commit()


def load_allowed_ids() -> set[int]:
    return set(config.ALLOWED_TELEGRAM_IDS)


def _drop_truncated_id_typos(ids: set[int]) -> list[int]:
    """Skip allowlist IDs that are a prefix of another ID (e.g. 203724579 vs 2037245798)."""
    sorted_ids = sorted(ids)
    kept: list[int] = []
    for uid in sorted_ids:
        uid_str = str(uid)
        if any(uid != other and str(other).startswith(uid_str) for other in sorted_ids):
            continue
        kept.append(uid)
    return kept


def is_allowed(user_id: int) -> bool:
    if not config.PAYWALL_ENABLED:
        return True
    return user_id in load_allowed_ids()


def broadcast_recipient_ids() -> list[int]:
    """Telegram user IDs that receive hourly trade DMs."""
    if not config.PAYWALL_ENABLED:
        ids = {row["telegram_id"] for row in list_subscribers()}
        ids.update(load_allowed_ids())
        return _drop_truncated_id_typos(ids)
    return _drop_truncated_id_typos(set(load_allowed_ids()))


def register_user(user_id: int, username: str | None = None) -> None:
    """Record a user who messaged the bot."""
    init_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    active = 1 if is_allowed(user_id) else 0

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscribers (telegram_id, username, first_seen, last_seen, active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = COALESCE(excluded.username, subscribers.username),
                last_seen = excluded.last_seen,
                active = excluded.active
            """,
            (user_id, username, now, now, active),
        )
        conn.commit()


def list_subscribers() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM subscribers ORDER BY last_seen DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def pending_subscribers() -> list[dict]:
    """Users who messaged the bot but are not on ALLOWED_TELEGRAM_IDS yet (paywall only)."""
    if not config.PAYWALL_ENABLED:
        return []
    allowed = load_allowed_ids()
    return [s for s in list_subscribers() if s["telegram_id"] not in allowed]


def active_subscribers() -> list[dict]:
    """Users who receive hourly DMs."""
    if not config.PAYWALL_ENABLED:
        return list_subscribers()
    allowed = load_allowed_ids()
    return [s for s in list_subscribers() if s["telegram_id"] in allowed]
