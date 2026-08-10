"""Bridge to the colocated trade_ideas mill database.

The mill mints the public volume ideas and sends their cards through THIS
bot's token (send-only). Telegram allows a single getUpdates consumer per
token and that consumer is this process, so the Accept/Reject callbacks land
here and are written back into the mill's SQLite.

Read-mostly and fail-soft: when IDEAS_DB is unset or the mill has not created
its database yet, every call reports "unavailable" and the bot carries on.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DecisionStatus = str  # recorded | duplicate | unknown_idea | unavailable


def ideas_db_path() -> Path | None:
    raw = (os.getenv("IDEAS_DB") or "").strip()
    if not raw:
        return None
    return Path(raw)


def enabled() -> bool:
    path = ideas_db_path()
    return path is not None and path.exists()


def _connect() -> sqlite3.Connection | None:
    path = ideas_db_path()
    if path is None or not path.exists():
        return None
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def get_idea(idea_id: int) -> dict[str, Any] | None:
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn:
            row = conn.execute(
                "SELECT id, source, product_id, direction, title FROM ideas WHERE id = ?",
                (idea_id,),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        logger.exception("trade_ideas bridge read failed for idea %s", idea_id)
        return None
    finally:
        conn.close()


def record_decision(idea_id: int, user_id: int, decision: str) -> DecisionStatus:
    """Write an Accept/Reject into the mill's decisions table."""
    if decision not in ("accept", "reject"):
        raise ValueError(f"invalid decision: {decision}")

    conn = _connect()
    if conn is None:
        return "unavailable"
    try:
        with conn:
            exists = conn.execute(
                "SELECT 1 FROM ideas WHERE id = ? LIMIT 1", (idea_id,)
            ).fetchone()
            if exists is None:
                return "unknown_idea"
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                conn.execute(
                    """
                    INSERT INTO decisions (idea_id, user_id, decision, decided_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (idea_id, user_id, decision, now),
                )
            except sqlite3.IntegrityError:
                return "duplicate"
        return "recorded"
    except sqlite3.Error:
        logger.exception("trade_ideas bridge write failed for idea %s", idea_id)
        return "unavailable"
    finally:
        conn.close()


def format_decision_reply(status: DecisionStatus, decision: str, idea_id: int) -> str:
    if status == "recorded":
        verb = "Accepted" if decision == "accept" else "Rejected"
        return f"{verb} idea #{idea_id}. Logged to your idea history."
    if status == "duplicate":
        return f"You already decided on idea #{idea_id}."
    if status == "unknown_idea":
        return f"Idea #{idea_id} is no longer available."
    return "Idea decisions are unavailable right now — try again shortly."
