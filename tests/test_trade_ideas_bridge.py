"""Bridge between the agent's callback dispatcher and the trade_ideas mill."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import trade_ideas_bridge

# Mirrors the mill's schema (trade_ideas/trade_ideas/store.py).
_MILL_SCHEMA = """
CREATE TABLE ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    product_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    title TEXT NOT NULL,
    blurb TEXT NOT NULL,
    signal_key TEXT NOT NULL UNIQUE,
    stance_context TEXT,
    confidence REAL,
    meta_json TEXT,
    status TEXT NOT NULL DEFAULT 'offered',
    created_at TEXT NOT NULL
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE (idea_id, user_id),
    FOREIGN KEY (idea_id) REFERENCES ideas(id)
);
"""


class TradeIdeasBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "ideas.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_MILL_SCHEMA)
        conn.execute(
            """
            INSERT INTO ideas
                (id, source, product_id, direction, title, blurb, signal_key, created_at)
            VALUES (5, 'news', 'ETH-USD', 'long', 'Fixture', 'blurb', 'news:1', '2026-08-10T00:00:00Z')
            """
        )
        conn.commit()
        conn.close()
        self._env = mock.patch.dict(
            "os.environ", {"IDEAS_DB": str(self.db_path)}, clear=False
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_enabled_when_db_present(self) -> None:
        self.assertTrue(trade_ideas_bridge.enabled())

    def test_records_accept_then_dedupes_per_user(self) -> None:
        self.assertEqual(
            trade_ideas_bridge.record_decision(5, 42, "accept"), "recorded"
        )
        self.assertEqual(
            trade_ideas_bridge.record_decision(5, 42, "reject"), "duplicate"
        )
        self.assertEqual(
            trade_ideas_bridge.record_decision(5, 43, "reject"), "recorded"
        )

    def test_unknown_idea(self) -> None:
        self.assertEqual(
            trade_ideas_bridge.record_decision(999, 42, "accept"), "unknown_idea"
        )

    def test_invalid_decision_rejected(self) -> None:
        with self.assertRaises(ValueError):
            trade_ideas_bridge.record_decision(5, 42, "maybe")

    def test_unavailable_when_env_unset(self) -> None:
        with mock.patch.dict("os.environ", {"IDEAS_DB": ""}, clear=False):
            self.assertFalse(trade_ideas_bridge.enabled())
            self.assertEqual(
                trade_ideas_bridge.record_decision(5, 42, "accept"), "unavailable"
            )

    def test_unavailable_when_db_missing(self) -> None:
        missing = Path(self._tmp.name) / "nope.db"
        with mock.patch.dict("os.environ", {"IDEAS_DB": str(missing)}, clear=False):
            self.assertEqual(
                trade_ideas_bridge.record_decision(5, 42, "accept"), "unavailable"
            )

    def test_get_idea(self) -> None:
        idea = trade_ideas_bridge.get_idea(5)
        self.assertIsNotNone(idea)
        self.assertEqual((idea or {})["product_id"], "ETH-USD")
        self.assertIsNone(trade_ideas_bridge.get_idea(999))

    def test_reply_copy_per_status(self) -> None:
        self.assertIn(
            "Accepted",
            trade_ideas_bridge.format_decision_reply("recorded", "accept", 5),
        )
        self.assertIn(
            "Rejected",
            trade_ideas_bridge.format_decision_reply("recorded", "reject", 5),
        )
        self.assertIn(
            "already decided",
            trade_ideas_bridge.format_decision_reply("duplicate", "accept", 5),
        )
        self.assertIn(
            "unavailable",
            trade_ideas_bridge.format_decision_reply("unavailable", "accept", 5),
        )


if __name__ == "__main__":
    unittest.main()
