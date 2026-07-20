"""Tests for personal paper accounts (Open account / My Metrics)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import paper
import user_books


class ContributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_path = Path(self._tmpdir.name) / "test_ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", self._db_path),
            patch.object(config, "PAPER_PORTFOLIO_VALUE", 5000.0),
            patch.object(bot_config, "PAPER_CONTRIBUTION_USD", 1000.0),
            patch.object(bot_config, "PAPER_ACCOUNT_DEFAULT_USD", 1000.0),
            patch.object(bot_config, "PAPER_ACCOUNT_SIZES", (500.0, 1000.0, 2500.0)),
        ]
        for item in self._patches:
            item.start()
        paper.init_db()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self._tmpdir.cleanup()

    def test_house_seed_counts_as_contribution(self) -> None:
        self.assertAlmostEqual(paper.total_contributed(), 5000.0, places=2)

    def test_fund_user_opens_personal_account_without_house_bump(self) -> None:
        result = paper.fund_user(111, username="alice")
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["amount_usd"], 1000.0, places=2)
        # House book unchanged.
        state = paper.get_state()
        self.assertAlmostEqual(float(state["cash_usd"]), 5000.0, places=2)
        self.assertAlmostEqual(paper.total_contributed(), 5000.0, places=2)
        account = user_books.get_account(111)
        assert account is not None
        self.assertEqual(account["starting_usd"], 1000.0)

    def test_fund_user_is_idempotent(self) -> None:
        paper.fund_user(111, username="alice")
        again = paper.fund_user(111, username="alice")
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "already_funded")

    def test_user_metrics_personal_book(self) -> None:
        paper.fund_user(111, username="alice")
        metrics = paper.get_user_metrics(
            111, spots={"ETH-USD": 2000.0, "BTC-USD": 40000.0}
        )
        self.assertTrue(metrics["ok"])
        self.assertAlmostEqual(metrics["equity_usd"], 1000.0, places=2)
        self.assertAlmostEqual(metrics["pnl_usd"], 0.0, places=2)

    def test_user_metrics_not_funded(self) -> None:
        metrics = paper.get_user_metrics(999)
        self.assertFalse(metrics["ok"])


if __name__ == "__main__":
    unittest.main()
