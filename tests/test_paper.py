"""Tests for paper position tracking."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import paper
from models import Suggestion


class PaperPositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_path = Path(self._tmpdir.name) / "test_ledger.db"
        self._config_patch = patch.object(config, "LEDGER_DB", self._db_path)
        self._config_patch.start()
        self._portfolio_patch = patch.object(config, "PAPER_PORTFOLIO_VALUE", 1000.0)
        self._portfolio_patch.start()
        paper.init_db()

    def tearDown(self) -> None:
        self._portfolio_patch.stop()
        self._config_patch.stop()
        self._tmpdir.cleanup()

    def test_open_position_stores_sl_and_tp(self) -> None:
        suggestion = Suggestion(
            action="deriv_sell",
            size=0.64,
            entry=1576.0,
            stop_loss=1592.0,
            take_profits=[1545.0, 1515.0, 1490.0],
            risk_reward=2.19,
            rationale="test short",
        )
        paper.update(suggestion, spot_price=1576.0, cycle_id="test_cycle_short")

        state = paper.get_state()
        self.assertEqual(state["side"], "short")
        self.assertEqual(state["action"], "deriv_sell")
        self.assertEqual(state["stop_loss"], 1592.0)
        self.assertEqual(state["take_profits"], [1545.0, 1515.0, 1490.0])
        self.assertEqual(state["open_cycle_id"], "test_cycle_short")
        self.assertIsNotNone(state["opened_at"])

    def test_format_position_detail_includes_exit_plan(self) -> None:
        paper.restore_open_position(
            action="deriv_sell",
            entry=1576.0,
            eth_qty=0.625,
            stop_loss=1592.0,
            take_profits=[1545.0, 1515.0, 1490.0],
            risk_reward=2.19,
            suggested_size=0.64,
            opened_at="2026-06-27T17:29:25Z",
            open_cycle_id="20260627T172925Z",
            spot_price=1560.0,
        )
        detail = paper.format_position_detail(1560.0)
        assert detail is not None
        self.assertIn("Stop loss: $1,592.00", detail)
        self.assertIn("Take profits: $1,545.00", detail)
        self.assertIn("Exit plan:", detail)
        self.assertIn("Unrealized P&L:", detail)

    def test_no_trade_does_not_clear_open_position(self) -> None:
        paper.restore_open_position(
            action="deriv_sell",
            entry=1576.0,
            eth_qty=0.625,
            stop_loss=1592.0,
            take_profits=[1545.0],
            risk_reward=2.0,
            suggested_size=0.64,
            opened_at="2026-06-27T17:29:25Z",
            open_cycle_id="cycle_a",
            spot_price=1570.0,
        )
        paper.update(Suggestion.no_trade("No setup"), spot_price=1570.0, cycle_id="cycle_b")
        self.assertTrue(paper.is_open())
        self.assertEqual(paper.get_state()["stop_loss"], 1592.0)


if __name__ == "__main__":
    unittest.main()
