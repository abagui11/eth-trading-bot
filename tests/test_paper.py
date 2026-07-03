"""Tests for paper position tracking."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import paper
from models import Suggestion


class PaperPositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_path = Path(self._tmpdir.name) / "test_ledger.db"
        self._config_patch = patch.object(config, "LEDGER_DB", self._db_path)
        self._config_patch.start()
        self._portfolio_patch = patch.object(config, "PAPER_PORTFOLIO_VALUE", 5000.0)
        self._portfolio_patch.start()
        self._max_patch = patch.object(bot_config, "MAX_OPEN_TRADES", 4)
        self._max_patch.start()
        paper.init_db()

    def tearDown(self) -> None:
        self._max_patch.stop()
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

    def test_restore_refuses_overwrite_without_force(self) -> None:
        paper.restore_open_position(
            action="deriv_sell",
            entry=1576.0,
            eth_qty=0.625,
            stop_loss=1592.0,
            take_profits=[1545.0],
            risk_reward=2.0,
            suggested_size=0.64,
            opened_at="2026-06-27T17:29:25Z",
            open_cycle_id="cycle_short",
            spot_price=1570.0,
        )
        with self.assertRaises(paper.OpenPositionConflictError):
            paper.restore_open_position(
                action="spot_buy",
                entry=1572.0,
                eth_qty=0.34,
                stop_loss=1543.0,
                take_profits=[1610.0],
                risk_reward=2.27,
                suggested_size=0.32,
                opened_at="2026-06-30T15:26:20Z",
                open_cycle_id="cycle_long",
                spot_price=1570.0,
            )
        self.assertEqual(paper.get_state()["open_cycle_id"], "cycle_short")

    def test_restore_force_closes_then_opens(self) -> None:
        paper.restore_open_position(
            action="deriv_sell",
            entry=1576.0,
            eth_qty=0.625,
            stop_loss=1592.0,
            take_profits=[1545.0],
            risk_reward=2.0,
            suggested_size=0.64,
            opened_at="2026-06-27T17:29:25Z",
            open_cycle_id="cycle_short",
            spot_price=1570.0,
        )
        paper.restore_open_position(
            action="spot_buy",
            entry=1572.0,
            eth_qty=0.34,
            stop_loss=1543.0,
            take_profits=[1610.0],
            risk_reward=2.27,
            suggested_size=0.32,
            opened_at="2026-06-30T15:26:20Z",
            open_cycle_id="cycle_long",
            spot_price=1570.0,
            force=True,
        )
        state = paper.get_state()
        self.assertEqual(state["side"], "long")
        self.assertEqual(state["open_cycle_id"], "cycle_long")

    def test_get_closed_trades_after_force_flip(self) -> None:
        paper.restore_open_position(
            action="deriv_sell",
            entry=1576.0,
            eth_qty=0.625,
            stop_loss=1592.0,
            take_profits=[1545.0],
            risk_reward=2.0,
            suggested_size=0.64,
            opened_at="2026-06-27T17:29:25Z",
            open_cycle_id="cycle_short",
            spot_price=1570.0,
        )
        paper.restore_open_position(
            action="spot_buy",
            entry=1572.0,
            eth_qty=0.34,
            stop_loss=1543.0,
            take_profits=[1610.0],
            risk_reward=2.27,
            suggested_size=0.32,
            opened_at="2026-06-30T15:26:20Z",
            open_cycle_id="cycle_long",
            spot_price=1578.48,
            force=True,
        )
        closed = paper.get_closed_trades()
        self.assertEqual(len(closed), 1)
        trade = closed[0]
        self.assertEqual(trade["side"], "short")
        self.assertEqual(trade["open_cycle_id"], "cycle_short")
        self.assertAlmostEqual(trade["entry"], 1576.0)
        self.assertAlmostEqual(trade["exit"], 1578.48)
        self.assertAlmostEqual(trade["realized_pnl_usd"], -1.55, places=2)

    def test_format_closed_trades_detail(self) -> None:
        import ledger as ledger_mod
        from models import Suggestion

        short = Suggestion(
            action="deriv_sell",
            size=0.64,
            entry=1576.0,
            stop_loss=1592.0,
            take_profits=[1545.0],
            risk_reward=2.0,
            rationale="short test",
        )
        ledger_mod.append(short, "cycle_short", 1576.0, "")

        paper.restore_open_position(
            action="deriv_sell",
            entry=1576.0,
            eth_qty=0.625,
            stop_loss=1592.0,
            take_profits=[1545.0],
            risk_reward=2.0,
            suggested_size=0.64,
            opened_at="2026-06-27T17:29:25Z",
            open_cycle_id="cycle_short",
            spot_price=1570.0,
        )
        paper.restore_open_position(
            action="spot_buy",
            entry=1572.0,
            eth_qty=0.34,
            stop_loss=1543.0,
            take_profits=[1610.0],
            risk_reward=2.27,
            suggested_size=0.32,
            opened_at="2026-06-30T15:26:20Z",
            open_cycle_id="cycle_long",
            spot_price=1578.48,
            force=True,
        )
        detail = paper.format_closed_trades_detail()
        assert detail is not None
        self.assertIn("DERIV_SELL", detail)
        self.assertIn("$1,576.00", detail)
        self.assertIn("realized -$", detail)

    def test_multiple_positions_stay_open(self) -> None:
        short = Suggestion(
            action="deriv_sell",
            size=0.64,
            entry=1576.0,
            stop_loss=1592.0,
            take_profits=[1545.0],
            risk_reward=2.0,
            rationale="short one",
        )
        long = Suggestion(
            action="spot_buy",
            size=0.32,
            entry=1572.0,
            stop_loss=1543.0,
            take_profits=[1610.0],
            risk_reward=2.27,
            rationale="long two",
        )
        paper.update(short, spot_price=1576.0, cycle_id="cycle_short")
        paper.update(long, spot_price=1572.0, cycle_id="cycle_long")
        positions = paper.get_open_positions(1570.0)
        self.assertEqual(len(positions), 2)
        cycles = {p["open_cycle_id"] for p in positions}
        self.assertEqual(cycles, {"cycle_short", "cycle_long"})

    def test_sl_closes_position_without_new_trade(self) -> None:
        paper.restore_open_position(
            action="deriv_sell",
            entry=1576.0,
            eth_qty=0.625,
            stop_loss=1592.0,
            take_profits=[1545.0],
            risk_reward=2.0,
            suggested_size=0.64,
            opened_at="2026-06-27T17:29:25Z",
            open_cycle_id="cycle_short",
            spot_price=1570.0,
        )
        paper.update(Suggestion.no_trade("No setup"), spot_price=1595.0, cycle_id="cycle_sl")
        self.assertFalse(paper.is_open())
        closed = paper.get_closed_trades(limit=1)
        self.assertEqual(closed[0]["close_reason"], "stop_loss")

    def test_fifo_closes_oldest_when_at_max(self) -> None:
        with patch.object(bot_config, "MAX_OPEN_TRADES", 2):
            for i in range(2):
                paper.update(
                    Suggestion(
                        action="deriv_sell",
                        size=0.5,
                        entry=1576.0 + i,
                        stop_loss=1600.0 + i,
                        take_profits=[1500.0],
                        risk_reward=2.0,
                        rationale=f"short {i}",
                    ),
                    spot_price=1576.0,
                    cycle_id=f"cycle_{i}",
                )
            paper.update(
                Suggestion(
                    action="spot_buy",
                    size=0.3,
                    entry=1570.0,
                    stop_loss=1540.0,
                    take_profits=[1620.0],
                    risk_reward=2.0,
                    rationale="third trade",
                ),
                spot_price=1570.0,
                cycle_id="cycle_new",
            )
        positions = paper.get_open_positions(1570.0)
        self.assertEqual(len(positions), 2)
        cycles = {p["open_cycle_id"] for p in positions}
        self.assertIn("cycle_new", cycles)
        self.assertNotIn("cycle_0", cycles)

    def test_compute_eth_qty_caps_at_max(self) -> None:
        with patch.object(config, "PAPER_PORTFOLIO_VALUE", 5000.0):
            qty = paper._compute_eth_qty(1700.0, 1717.0, cash=5000.0)
        self.assertAlmostEqual(qty, bot_config.MAX_ETH_QTY, places=4)

    def test_compute_eth_qty_raises_to_min_when_affordable(self) -> None:
        with patch.object(config, "PAPER_PORTFOLIO_VALUE", 5000.0):
            # Wide stop → small risk-based size; should bump to MIN_ETH_QTY.
            qty = paper._compute_eth_qty(1700.0, 1200.0, cash=5000.0)
        self.assertAlmostEqual(qty, bot_config.MIN_ETH_QTY, places=4)

    def test_archive_epoch_and_reset(self) -> None:
        paper.update(
            Suggestion(
                action="deriv_sell",
                size=0.5,
                entry=1576.0,
                stop_loss=1592.0,
                take_profits=[1545.0],
                risk_reward=2.0,
                rationale="pre-archive",
            ),
            spot_price=1576.0,
            cycle_id="pre_archive",
        )
        summary = paper.archive_epoch_and_reset(
            starting_usd=5000.0,
            epoch_label="test_5k",
            prior_epoch_label="legacy_test",
        )
        self.assertGreater(summary["archived_trade_rows"], 0)
        state = paper.get_state()
        self.assertEqual(float(state["starting_usd"]), 5000.0)
        self.assertEqual(float(state["cash_usd"]), 5000.0)
        self.assertFalse(paper.is_open())
        archived = paper.get_archived_closed_trades(limit=5)
        self.assertEqual(len(archived), 0)  # position still open at archive time
        epoch = paper.get_epoch_info()
        self.assertEqual(epoch["epoch_label"], "test_5k")
        self.assertEqual(epoch["prior_epoch_count"], 1)


if __name__ == "__main__":
    unittest.main()
