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
    entry REAL,
    stop_loss REAL,
    take_profits_json TEXT,
    risk_reward REAL,
    chart_path TEXT,
    sent_at TEXT,
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
                (id, source, product_id, direction, title, blurb, signal_key,
                 entry, stop_loss, take_profits_json, created_at)
            VALUES (5, 'news', 'ETH-USD', 'long', 'Fixture', 'blurb', 'news:1',
                    100.0, 95.0, '[110.0, 120.0]', '2026-08-10T00:00:00Z')
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

    def test_accept_opens_user_paper_trade(self) -> None:
        self.assertEqual(
            trade_ideas_bridge.record_decision(5, 42, "accept"), "recorded"
        )
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM user_paper_trades WHERE user_id = 42 AND idea_id = 5"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["entry"], 100.0)
        self.assertEqual(row["take_profit"], 110.0)
        self.assertEqual(row["status"], "open")

    def test_reject_does_not_open_user_paper(self) -> None:
        self.assertEqual(
            trade_ideas_bridge.record_decision(5, 42, "reject"), "recorded"
        )
        conn = sqlite3.connect(self.db_path)
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "user_paper_trades" in tables:
            n = conn.execute("SELECT COUNT(*) FROM user_paper_trades").fetchone()[0]
            self.assertEqual(n, 0)
        conn.close()

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
            "/me",
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

    def test_volume_book_report_marks_open_trades(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idea_id INTEGER NOT NULL UNIQUE,
                product_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry REAL,
                stop_loss REAL,
                take_profit REAL,
                take_profits_json TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                exit_price REAL,
                pnl_pct REAL,
                opened_at TEXT NOT NULL,
                closed_at TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO paper_trades
                (idea_id, product_id, direction, entry, stop_loss, take_profit,
                 status, opened_at)
            VALUES (5, 'ETH-USD', 'short', 1900.0, 1930.0, 1855.0,
                    'open', '2026-08-11T00:00:00Z')
            """
        )
        conn.commit()
        conn.close()

        report = trade_ideas_bridge.volume_book_report({"ETH-USD": 1870.0})
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report["open"], 1)
        self.assertGreater(report["unrealized_pct_sum"], 0)
        text = trade_ideas_bridge.format_volume_book_report(report)
        self.assertIn("Volume idea book", text)
        self.assertIn("Unrealized", text)
        self.assertIn("Open positions", text)
        self.assertIn("/me", text)

    def test_user_book_report(self) -> None:
        self.assertEqual(
            trade_ideas_bridge.record_decision(5, 42, "accept"), "recorded"
        )
        report = trade_ideas_bridge.user_book_report(42, {"ETH-USD": 105.0})
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report["lane"], "personal")
        self.assertEqual(report["open"], 1)
        self.assertGreater(report["unrealized_pct_sum"], 0)
        text = trade_ideas_bridge.format_user_book_report(report)
        self.assertIn("Your idea portfolio", text)
        self.assertIn("/performance", text)
        keyboard = trade_ideas_bridge.user_book_close_keyboard(report)
        self.assertIsNotNone(keyboard)

    def test_manual_close_at_spot(self) -> None:
        self.assertEqual(
            trade_ideas_bridge.record_decision(5, 42, "accept"), "recorded"
        )
        report = trade_ideas_bridge.user_book_report(42, {"ETH-USD": 105.0})
        assert report is not None
        paper_id = int(report["open_trades"][0]["id"])
        status, closed = trade_ideas_bridge.close_user_trade_at_spot(
            42, paper_id, 108.0
        )
        self.assertEqual(status, "closed")
        assert closed is not None
        self.assertEqual(closed["status"], "manual")
        self.assertGreater(closed["pnl_pct"], 0)
        # Second close fails
        status2, _ = trade_ideas_bridge.close_user_trade_at_spot(42, paper_id, 108.0)
        self.assertEqual(status2, "not_found")
        # Other user cannot close someone else's row
        self.assertEqual(
            trade_ideas_bridge.record_decision(5, 99, "accept"), "recorded"
        )
        report99 = trade_ideas_bridge.user_book_report(99, {"ETH-USD": 105.0})
        assert report99 is not None
        other_id = int(report99["open_trades"][0]["id"])
        status3, _ = trade_ideas_bridge.close_user_trade_at_spot(42, other_id, 108.0)
        self.assertEqual(status3, "not_found")
        self.assertIn(
            "Closed",
            trade_ideas_bridge.format_close_reply("closed", closed),
        )


if __name__ == "__main__":
    unittest.main()
