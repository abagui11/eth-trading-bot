"""Tests for macro store and context."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import bot_config
from macro import store
from macro.context import active_posture, build_macro_block


class TestMacroStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.LEDGER_DB
        config.LEDGER_DB = Path(self._tmp.name) / "test_ledger.db"
        store.init_db()

    def tearDown(self) -> None:
        config.LEDGER_DB = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_insert_and_list(self) -> None:
        event = store.insert_event(
            source="test",
            title="Iran oil tanker attacks in Hormuz",
            url="https://example.com/1",
            summary=None,
            published_at=None,
            keyword_score=80,
            keyword_hits=[{"rule": "T1_PHRASE", "term": "tanker attacks", "points": 40}],
            severity=4,
            eth_bias="bearish",
            category="geopolitical_energy",
            eth_impact_summary="risk-off",
            posture_hints=["avoid_new_long"],
            expires_at="2099-01-01T00:00:00Z",
            status="classified",
        )
        self.assertIsNotNone(event.get("id"))
        rows = store.list_events(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["severity"], 4)

    def test_active_posture_gate_long(self) -> None:
        store.insert_event(
            source="test",
            title="High impact bearish headline",
            url="https://example.com/2",
            summary=None,
            published_at=None,
            keyword_score=80,
            keyword_hits=[],
            severity=4,
            eth_bias="bearish",
            category="test",
            eth_impact_summary="down",
            posture_hints=[],
            expires_at="2099-01-01T00:00:00Z",
            status="classified",
        )
        with mock.patch.object(bot_config, "MACRO_CONTEXT_ENABLED", True):
            posture = active_posture()
        self.assertTrue(posture["gate_long"])
        self.assertEqual(posture["eth_bias"], "bearish")
        block = build_macro_block()
        self.assertIn("Macro context", block)
        self.assertIn("bearish", block)


if __name__ == "__main__":
    unittest.main()
