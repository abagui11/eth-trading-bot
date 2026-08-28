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

    # -- operator Accept → live clip -----------------------------------------

    def _operator(self) -> int:
        import bot_config

        return bot_config.LIVE_MILL_FILL_TELEGRAM_IDS[0]

    def test_only_listed_operators_can_fill(self) -> None:
        self.assertTrue(trade_ideas_bridge.is_fill_operator(self._operator()))
        self.assertFalse(trade_ideas_bridge.is_fill_operator(42))
        verdict = trade_ideas_bridge.request_manual_fill(5, 42)
        self.assertEqual(verdict["skip_reason"], "not_authorized")

    def test_manual_fill_passes_idea_levels_to_the_executor(self) -> None:
        with mock.patch(
            "execute.execute_mill_idea", return_value={"executed": True}
        ) as mocked:
            trade_ideas_bridge.request_manual_fill(5, self._operator())
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["product_id"], "ETH-USD")
        self.assertEqual(kwargs["entry"], 100.0)
        self.assertEqual(kwargs["stop_loss"], 95.0)
        self.assertEqual(kwargs["take_profits"], [110.0, 120.0])
        self.assertEqual(kwargs["signal_key"], "news:1")
        self.assertEqual(kwargs["fill_type"], "manual")
        self.assertEqual(kwargs["accepted_by"], self._operator())

    def test_filled_idea_is_tagged_manual_in_the_mill_db(self) -> None:
        with mock.patch("execute.execute_mill_idea", return_value={"executed": True}):
            trade_ideas_bridge.request_manual_fill(5, self._operator())
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM ideas WHERE id = 5").fetchone()
        conn.close()
        self.assertEqual(row["live_fill_type"], "manual")
        self.assertEqual(row["live_filled_by"], self._operator())

    def test_unfilled_idea_is_not_tagged(self) -> None:
        with mock.patch(
            "execute.execute_mill_idea",
            return_value={"executed": False, "skip_reason": "sleeve_full"},
        ):
            verdict = trade_ideas_bridge.request_manual_fill(5, self._operator())
        self.assertEqual(verdict["skip_reason"], "sleeve_full")
        conn = sqlite3.connect(self.db_path)
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(ideas)")}
        if "live_fill_type" in cols:
            row = conn.execute(
                "SELECT live_fill_type FROM ideas WHERE id = 5"
            ).fetchone()
            self.assertIsNone(row[0])
        conn.close()

    def test_manual_fill_needs_sized_levels(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO ideas
                (id, source, product_id, direction, title, blurb, signal_key,
                 created_at)
            VALUES (77, 'spike', 'ETH-USD', 'long', 'No levels', 'x',
                    'spike:unsized', '2026-08-27T12:00:00Z')
            """
        )
        conn.commit()
        conn.close()
        verdict = trade_ideas_bridge.request_manual_fill(77, self._operator())
        self.assertEqual(verdict["skip_reason"], "unsized")

    def test_sleeve_full_reply_names_the_open_trades(self) -> None:
        text = trade_ideas_bridge.format_manual_fill_reply(
            {
                "executed": False,
                "skip_reason": "sleeve_full",
                "capacity": {
                    "open": 3,
                    "max_open": 3,
                    "open_trades": [
                        {
                            "id": 12,
                            "product_id": "BTC-USD",
                            "side": "short",
                            "entry": 90000.0,
                            "fill_type": "manual",
                        }
                    ],
                },
            },
            9,
        )
        assert text is not None
        self.assertIn("Too many trades open", text)
        self.assertIn("#12 BTC-USD short", text)
        self.assertIn("Close one to free a slot", text)

    def test_get_idea(self) -> None:
        idea = trade_ideas_bridge.get_idea(5)
        self.assertIsNotNone(idea)
        self.assertEqual((idea or {})["product_id"], "ETH-USD")
        self.assertIsNone(trade_ideas_bridge.get_idea(999))

    def test_idea_stream_skips_unsized_and_polls_after_id(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO ideas
                (id, source, product_id, direction, title, blurb, signal_key,
                 created_at)
            VALUES (6, 'spike', 'SOL-USD', 'long', 'No levels', 'x', 'spike:1',
                    '2026-08-27T12:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO ideas
                (id, source, product_id, direction, title, blurb, signal_key,
                 entry, stop_loss, take_profits_json, sent_at, created_at)
            VALUES (7, 'session', 'BTC-USD', 'short', 'NY open', 'setup',
                    'session:1', 65000.0, 66000.0, '[64000.0]',
                    NULL, '2026-08-27T13:00:00Z')
            """
        )
        conn.commit()
        conn.close()

        payload = trade_ideas_bridge.idea_stream(limit=40)
        self.assertIsNotNone(payload)
        assert payload is not None
        ids = [c["id"] for c in payload["ideas"]]
        self.assertIn(5, ids)
        self.assertIn(7, ids)
        self.assertNotIn(6, ids)
        card = next(c for c in payload["ideas"] if c["id"] == 5)
        self.assertEqual(card["source_label"], "NEWS")
        self.assertFalse(card["telegram_sent"])
        self.assertNotIn("chart_path", card)
        self.assertNotIn("signal_key", card)

        newer = trade_ideas_bridge.idea_stream(after_id=5)
        assert newer is not None
        self.assertEqual([c["id"] for c in newer["ideas"]], [7])

    def test_idea_funnel_counts_mint_vs_telegram_vs_decisions(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE ideas SET created_at = '2026-08-27T01:00:00Z' WHERE id = 5"
        )
        conn.execute(
            """
            INSERT INTO ideas
                (id, source, product_id, direction, title, blurb, signal_key,
                 entry, stop_loss, sent_at, created_at)
            VALUES (8, 'news', 'ETH-USD', 'long', 'Sent', 'b', 'news:2',
                    100.0, 95.0, '2026-08-27T02:00:00Z', '2026-08-27T02:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO decisions (idea_id, user_id, decision, decided_at)
            VALUES (5, 42, 'accept', '2026-08-27T03:00:00Z'),
                   (8, 42, 'reject', '2026-08-27T03:01:00Z'),
                   (8, 7, 'accept', '2026-08-27T03:02:00Z')
            """
        )
        conn.commit()
        conn.close()

        funnel = trade_ideas_bridge.idea_funnel(day="2026-08-27")
        self.assertIsNotNone(funnel)
        assert funnel is not None
        self.assertEqual(funnel["minted"], 2)
        self.assertEqual(funnel["sized"], 2)
        self.assertEqual(funnel["telegram_sent"], 1)
        self.assertEqual(funnel["not_on_telegram"], 1)
        self.assertEqual(funnel["accepts"], 2)
        self.assertEqual(funnel["rejects"], 1)
        self.assertEqual(funnel["unique_users"], 2)

    def test_idea_stream_attaches_my_decision(self) -> None:
        trade_ideas_bridge.record_decision(5, 42, "accept")
        payload = trade_ideas_bridge.idea_stream(user_id=42)
        assert payload is not None
        card = next(c for c in payload["ideas"] if c["id"] == 5)
        self.assertEqual(card["my_decision"], "accept")
        other = trade_ideas_bridge.idea_stream(user_id=99)
        assert other is not None
        self.assertIsNone(next(c for c in other["ideas"] if c["id"] == 5)["my_decision"])

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
