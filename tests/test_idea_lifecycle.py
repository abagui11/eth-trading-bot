"""Idea expiry and the re-offer sweep.

Two gaps this closes: a card used to stay acceptable forever, and a mill clip
closing left the sleeve idle because auto-fill only ever fired at mint.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_config  # noqa: E402
import trade_ideas_bridge as bridge  # noqa: E402

_IDEAS_SCHEMA = """
CREATE TABLE ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'test',
    product_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    blurb TEXT NOT NULL DEFAULT '',
    signal_key TEXT NOT NULL UNIQUE,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'offered',
    created_at TEXT NOT NULL,
    entry REAL,
    stop_loss REAL,
    take_profits_json TEXT,
    sent_at TEXT,
    live_fill_type TEXT,
    live_filled_by INTEGER
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE (idea_id, user_id)
);
"""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class IdeasDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "ideas.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript(_IDEAS_SCHEMA)
        self._env = patch.dict(os.environ, {"IDEAS_DB": str(self.db)})
        self._env.start()
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _idea(self, *, minutes_ago: int = 0, **overrides) -> int:
        row = {
            "product_id": "ETH-USD",
            "direction": "long",
            "signal_key": f"sig-{overrides.pop('key', minutes_ago)}",
            "confidence": 0.7,
            "status": "sent",
            "created_at": _iso(self.now - timedelta(minutes=minutes_ago)),
            "sent_at": _iso(self.now - timedelta(minutes=minutes_ago)),
            "entry": 2411.5,
            "stop_loss": 2385.0,
            "take_profits_json": "[2440.0, 2477.0]",
        }
        row.update(overrides)
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with sqlite3.connect(self.db) as conn:
            cur = conn.execute(
                f"INSERT INTO ideas ({cols}) VALUES ({marks})", tuple(row.values())
            )
            return int(cur.lastrowid)

    def _status(self, idea_id: int) -> str:
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT status FROM ideas WHERE id = ?", (idea_id,)
            ).fetchone()[0]


class HubColumnMigrationTests(unittest.TestCase):
    """The mill's own migration may never have run: /opt/trade-ideas is not a
    git checkout, so the hub adds the columns it writes rather than erroring."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "ideas.db"
        legacy = _IDEAS_SCHEMA.replace(
            "    live_fill_type TEXT,\n    live_filled_by INTEGER\n", "    sent_at2 TEXT\n"
        ).replace("    sent_at TEXT,\n    sent_at2 TEXT\n", "    sent_at TEXT\n")
        with sqlite3.connect(self.db) as conn:
            conn.executescript(legacy)
        self._env = patch.dict(os.environ, {"IDEAS_DB": str(self.db)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _columns(self) -> set[str]:
        with sqlite3.connect(self.db) as conn:
            return {r[1] for r in conn.execute("PRAGMA table_info(ideas)")}

    def test_missing_columns_are_added_on_connect(self) -> None:
        self.assertNotIn("live_fill_type", self._columns())
        bridge._connect().close()
        self.assertIn("live_fill_type", self._columns())
        self.assertIn("live_filled_by", self._columns())

    def test_expiry_works_against_a_legacy_database(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO ideas (product_id, direction, signal_key, status, "
                "created_at, sent_at) VALUES ('ETH-USD','long','k','sent',?,?)",
                ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"),
            )
        self.assertEqual(bridge.expire_stale_ideas(15), 1)


class ExpiryTests(IdeasDbTestCase):
    def test_a_card_older_than_the_window_expires(self) -> None:
        old = self._idea(minutes_ago=20)
        self.assertEqual(bridge.expire_stale_ideas(15), 1)
        self.assertEqual(self._status(old), "expired")

    def test_a_fresh_card_is_left_alone(self) -> None:
        fresh = self._idea(minutes_ago=5)
        self.assertEqual(bridge.expire_stale_ideas(15), 0)
        self.assertEqual(self._status(fresh), "sent")

    def test_an_accepted_card_is_never_expired(self) -> None:
        """Someone acted on it, so the clock is not what decides its fate."""
        accepted = self._idea(minutes_ago=90)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO decisions (idea_id, user_id, decision, decided_at) "
                "VALUES (?, ?, 'accept', ?)",
                (accepted, 111, _iso(self.now)),
            )
        bridge.expire_stale_ideas(15)
        self.assertEqual(self._status(accepted), "sent")

    def test_a_rejected_card_still_expires(self) -> None:
        rejected = self._idea(minutes_ago=90)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO decisions (idea_id, user_id, decision, decided_at) "
                "VALUES (?, ?, 'reject', ?)",
                (rejected, 111, _iso(self.now)),
            )
        bridge.expire_stale_ideas(15)
        self.assertEqual(self._status(rejected), "expired")

    def test_a_card_that_already_filled_live_is_left_alone(self) -> None:
        filled = self._idea(minutes_ago=90, live_fill_type="auto")
        bridge.expire_stale_ideas(15)
        self.assertEqual(self._status(filled), "sent")

    def test_the_sweep_is_idempotent(self) -> None:
        self._idea(minutes_ago=20)
        self.assertEqual(bridge.expire_stale_ideas(15), 1)
        self.assertEqual(bridge.expire_stale_ideas(15), 0)

    def test_the_mills_own_retired_marker_is_not_overwritten(self) -> None:
        """'retired' is the mill's word for undeliverable; it is not our expiry."""
        retired = self._idea(minutes_ago=999, status="retired")
        bridge.expire_stale_ideas(15)
        self.assertEqual(self._status(retired), "retired")

    def test_a_zero_window_disables_expiry(self) -> None:
        old = self._idea(minutes_ago=999)
        self.assertEqual(bridge.expire_stale_ideas(0), 0)
        self.assertEqual(self._status(old), "sent")

    def test_a_late_accept_on_an_expired_card_is_refused(self) -> None:
        old = self._idea(minutes_ago=20)
        bridge.expire_stale_ideas(15)
        verdict = bridge.request_manual_fill(old, 8282981740)
        self.assertFalse(verdict["executed"])
        self.assertEqual(verdict["skip_reason"], "expired")

    def test_the_operator_is_told_the_card_expired(self) -> None:
        reply = bridge.format_manual_fill_reply(
            {"executed": False, "skip_reason": "expired", "capacity": {}}, 42
        )
        self.assertIn("expired", reply)
        self.assertIn("NOT filled", reply)


class ReofferCandidateTests(IdeasDbTestCase):
    def test_filled_and_retired_cards_are_not_candidates(self) -> None:
        self._idea(key="b", minutes_ago=6, live_fill_type="manual")
        self._idea(key="d", minutes_ago=4, status="retired")
        good = self._idea(key="c", minutes_ago=7)
        self.assertEqual([c["id"] for c in bridge.reoffer_candidates()], [good])

    def test_an_expired_card_can_still_be_swept(self) -> None:
        """Expiry stops a person tapping Accept; the sweep re-prices instead."""
        expired = self._idea(key="e", minutes_ago=30, status="expired")
        self.assertEqual([c["id"] for c in bridge.reoffer_candidates()], [expired])

    def test_cards_beyond_the_sweep_lookback_are_not_candidates(self) -> None:
        self._idea(key="ancient", minutes_ago=60 * 24)
        with patch.object(bot_config, "LIVE_MILL_REOFFER_MAX_AGE_MIN", 120):
            self.assertEqual(bridge.reoffer_candidates(), [])

    def test_low_conviction_cards_are_not_candidates(self) -> None:
        self._idea(key="weak", minutes_ago=5, confidence=0.2)
        good = self._idea(key="strong", minutes_ago=6, confidence=0.8)
        self.assertEqual([c["id"] for c in bridge.reoffer_candidates()], [good])

    def test_unsized_cards_are_not_candidates(self) -> None:
        self._idea(key="unsized", minutes_ago=5, entry=None, stop_loss=None)
        self.assertEqual(bridge.reoffer_candidates(), [])

    def test_freshest_first(self) -> None:
        old = self._idea(key="old", minutes_ago=60)
        new = self._idea(key="new", minutes_ago=2)
        self.assertEqual([c["id"] for c in bridge.reoffer_candidates()], [new, old])


class SweepTests(IdeasDbTestCase):
    def test_the_first_idea_that_still_validates_is_filled(self) -> None:
        stale = self._idea(key="stale", minutes_ago=2)
        good = self._idea(key="good", minutes_ago=30)
        calls: list[int] = []

        def fake(**kw):
            calls.append(kw["idea_id"])
            if kw["idea_id"] == stale:
                return {"executed": False, "skip_reason": "chased"}
            return {"executed": True, "result": {"mode": "live"}}

        with patch("execute.execute_mill_idea", side_effect=fake):
            verdict = bridge.sweep_reoffer()

        self.assertTrue(verdict["executed"])
        self.assertEqual(calls, [stale, good])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT live_fill_type FROM ideas WHERE id = ?", (good,)
                ).fetchone()[0],
                "auto",
            )

    def test_the_sweep_stops_if_the_sleeve_is_no_longer_empty(self) -> None:
        self._idea(key="a", minutes_ago=2)
        self._idea(key="b", minutes_ago=3)
        with patch(
            "execute.execute_mill_idea",
            return_value={"executed": False, "skip_reason": "book_not_empty"},
        ) as ex:
            self.assertIsNone(bridge.sweep_reoffer())
        self.assertEqual(ex.call_count, 1)

    def test_nothing_to_offer_is_not_an_error(self) -> None:
        with patch("execute.execute_mill_idea") as ex:
            self.assertIsNone(bridge.sweep_reoffer())
        ex.assert_not_called()

    def test_the_sweep_can_be_disabled(self) -> None:
        self._idea(key="a", minutes_ago=2)
        with patch.object(bot_config, "LIVE_MILL_REOFFER_ENABLED", False), \
             patch("execute.execute_mill_idea") as ex:
            self.assertIsNone(bridge.sweep_reoffer())
        ex.assert_not_called()

    def test_every_candidate_is_offered_as_an_auto_fill(self) -> None:
        """The sweep is the FIFO path refilling itself, not an operator Accept."""
        self._idea(key="a", minutes_ago=2)
        with patch(
            "execute.execute_mill_idea",
            return_value={"executed": False, "skip_reason": "chased"},
        ) as ex:
            bridge.sweep_reoffer()
        self.assertEqual(ex.call_args.kwargs["fill_type"], "auto")


if __name__ == "__main__":
    unittest.main()
