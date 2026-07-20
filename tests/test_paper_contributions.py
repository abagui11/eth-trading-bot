"""Tests for personal paper accounts (replaces shared Fund ownership model)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import paper
import user_books


class PaperAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_path = Path(self._tmpdir.name) / "test_ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", self._db_path),
            patch.object(config, "PAPER_PORTFOLIO_VALUE", 5000.0),
            patch.object(bot_config, "PAPER_CONTRIBUTION_USD", 1000.0),
            patch.object(bot_config, "PAPER_ACCOUNT_DEFAULT_USD", 1000.0),
            patch.object(bot_config, "PAPER_ACCOUNT_SIZES", (500.0, 1000.0, 2500.0)),
            patch.object(bot_config, "HOUSE_CONTRIBUTION_TELEGRAM_ID", 0),
        ]
        for item in self._patches:
            item.start()
        paper.init_db()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self._tmpdir.cleanup()

    def test_fund_user_opens_personal_account(self) -> None:
        first = paper.fund_user(12345, "alice")
        self.assertTrue(first["ok"])
        self.assertEqual(first["amount"], 1000.0)
        # House cash unchanged (still seeded at 5000).
        state = paper.get_state()
        self.assertAlmostEqual(float(state["cash_usd"]), 5000.0)

        second = paper.fund_user(12345, "changed-name")
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "already_funded")

        account = user_books.get_account(12345)
        assert account is not None
        self.assertEqual(account["starting_usd"], 1000.0)

    def test_get_user_metrics_personal_book(self) -> None:
        paper.fund_user(12345, "alice")
        metrics = paper.get_user_metrics(
            12345,
            spots={"ETH-USD": 3000.0, "BTC-USD": 60000.0},
        )
        self.assertTrue(metrics["ok"])
        self.assertAlmostEqual(metrics["equity_usd"], 1000.0)
        self.assertAlmostEqual(metrics["pnl_usd"], 0.0)

    def test_get_user_metrics_requires_account(self) -> None:
        result = paper.get_user_metrics(
            999,
            spots={"ETH-USD": 3000.0, "BTC-USD": 60000.0},
        )
        self.assertEqual(result, {"ok": False, "reason": "not_funded"})


if __name__ == "__main__":
    unittest.main()
