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
import validate
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
        # Outcome chart render hits Coinbase OHLC — keep unit tests offline.
        self._outcome_patch = patch.object(paper, "flush_pending_outcome_charts")
        self._outcome_patch.start()
        paper.init_db()

    def tearDown(self) -> None:
        self._outcome_patch.stop()
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
        validate.validate_trade_risk(suggestion, portfolio_value=5000.0)
        paper.update(suggestion, spot_price=1576.0, cycle_id="test_cycle_short")

        state = paper.get_state()
        self.assertEqual(state["side"], "short")
        self.assertEqual(state["action"], "deriv_sell")
        self.assertAlmostEqual(state["eth_qty"], suggestion.size, places=4)
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

    def test_opposite_signal_partially_nets_long(self) -> None:
        paper.restore_open_position(
            action="spot_buy",
            entry=1800.0,
            eth_qty=1.0,
            stop_loss=1700.0,
            take_profits=[1900.0],
            risk_reward=1.0,
            suggested_size=1.0,
            opened_at="2026-07-07T12:00:00Z",
            open_cycle_id="cycle_long",
            spot_price=1800.0,
        )
        short = Suggestion(
            action="deriv_sell",
            size=0.5,
            entry=1850.0,
            stop_loss=1950.0,
            take_profits=[1750.0],
            risk_reward=1.0,
            rationale="short hedge",
        )
        validate.validate_trade_risk(short, portfolio_value=5000.0)
        paper.update(short, spot_price=1850.0, cycle_id="cycle_short")

        state = paper.get_state()
        self.assertEqual(state["side"], "long")
        self.assertAlmostEqual(state["eth_qty"], 1.0 - short.size, places=4)
        self.assertEqual(state["open_cycle_id"], "cycle_long")
        self.assertEqual(state["stop_loss"], 1700.0)
        positions = paper.get_open_positions(1850.0)
        self.assertEqual(len(positions), 1)

    def test_opposite_signal_full_offset_goes_flat(self) -> None:
        paper.restore_open_position(
            action="spot_buy",
            entry=1800.0,
            eth_qty=0.5,
            stop_loss=1700.0,
            take_profits=[1900.0],
            risk_reward=1.0,
            suggested_size=0.5,
            opened_at="2026-07-07T12:00:00Z",
            open_cycle_id="cycle_long",
            spot_price=1800.0,
        )
        short = Suggestion(
            action="deriv_sell",
            size=0.5,
            entry=1850.0,
            stop_loss=1950.0,
            take_profits=[1750.0],
            risk_reward=1.0,
            rationale="short",
        )
        validate.validate_trade_risk(short, portfolio_value=5000.0)
        paper.update(short, spot_price=1850.0, cycle_id="cycle_short")

        self.assertFalse(paper.is_open())
        closed = paper.get_closed_trades(limit=2)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["close_reason"], "signal_net")

    def test_opposite_signal_flips_to_short(self) -> None:
        paper.restore_open_position(
            action="spot_buy",
            entry=1800.0,
            eth_qty=0.25,
            stop_loss=1700.0,
            take_profits=[1900.0],
            risk_reward=1.0,
            suggested_size=0.25,
            opened_at="2026-07-07T12:00:00Z",
            open_cycle_id="cycle_long",
            spot_price=1800.0,
        )
        short = Suggestion(
            action="deriv_sell",
            size=1.0,
            entry=1850.0,
            stop_loss=1900.0,
            take_profits=[1750.0],
            risk_reward=1.0,
            rationale="short flip",
        )
        validate.validate_trade_risk(short, portfolio_value=5000.0)
        paper.update(short, spot_price=1850.0, cycle_id="cycle_short")

        state = paper.get_state()
        self.assertEqual(state["side"], "short")
        self.assertAlmostEqual(state["eth_qty"], short.size - 0.25, places=4)
        self.assertEqual(state["stop_loss"], 1900.0)
        self.assertEqual(state["open_cycle_id"], "cycle_short")

    def test_short_then_long_nets_same_way(self) -> None:
        paper.restore_open_position(
            action="deriv_sell",
            entry=1850.0,
            eth_qty=1.0,
            stop_loss=1950.0,
            take_profits=[1750.0],
            risk_reward=1.0,
            suggested_size=1.0,
            opened_at="2026-07-07T12:00:00Z",
            open_cycle_id="cycle_short",
            spot_price=1850.0,
        )
        long = Suggestion(
            action="spot_buy",
            size=0.5,
            entry=1800.0,
            stop_loss=1700.0,
            take_profits=[1900.0],
            risk_reward=1.0,
            rationale="long hedge",
        )
        validate.validate_trade_risk(long, portfolio_value=5000.0)
        paper.update(long, spot_price=1800.0, cycle_id="cycle_long")

        state = paper.get_state()
        self.assertEqual(state["side"], "short")
        self.assertAlmostEqual(state["eth_qty"], 1.0 - long.size, places=4)

    def test_same_direction_adds_to_position(self) -> None:
        first = Suggestion(
            action="spot_buy",
            size=0.5,
            entry=1800.0,
            stop_loss=1700.0,
            take_profits=[1900.0],
            risk_reward=1.0,
            rationale="long one",
        )
        second = Suggestion(
            action="spot_buy",
            size=0.5,
            entry=1850.0,
            stop_loss=1750.0,
            take_profits=[1950.0],
            risk_reward=1.0,
            rationale="long two",
        )
        validate.validate_trade_risk(first, portfolio_value=5000.0)
        validate.validate_trade_risk(second, portfolio_value=5000.0)
        paper.update(first, spot_price=1800.0, cycle_id="cycle_long_1")
        paper.update(second, spot_price=1850.0, cycle_id="cycle_long_2")

        state = paper.get_state()
        self.assertEqual(state["side"], "long")
        self.assertAlmostEqual(state["eth_qty"], first.size + second.size, places=4)
        expected_entry = (first.size * 1800.0 + second.size * 1850.0) / (
            first.size + second.size
        )
        self.assertAlmostEqual(state["avg_entry"], expected_entry, places=2)
        self.assertEqual(state["stop_loss"], 1750.0)
        positions = paper.get_open_positions(1850.0)
        self.assertEqual(len(positions), 1)

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

    def test_opposite_signal_reduces_existing_short_exposure(self) -> None:
        paper.restore_open_position(
            action="deriv_sell",
            entry=1576.0,
            eth_qty=1.0,
            stop_loss=1600.0,
            take_profits=[1500.0],
            risk_reward=2.0,
            suggested_size=1.0,
            opened_at="2026-07-07T12:00:00Z",
            open_cycle_id="cycle_short",
            spot_price=1576.0,
        )
        long = Suggestion(
            action="spot_buy",
            size=0.3,
            entry=1570.0,
            stop_loss=1540.0,
            take_profits=[1620.0],
            risk_reward=2.0,
            rationale="long hedge",
        )
        validate.validate_trade_risk(long, portfolio_value=1000.0)
        paper.update(long, spot_price=1570.0, cycle_id="cycle_new")

        positions = paper.get_open_positions(1570.0)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["side"], "short")
        self.assertAlmostEqual(positions[0]["eth_qty"], 1.0 - long.size, places=4)

    def test_get_sizing_basis_uses_live_equity(self) -> None:
        paper.restore_open_position(
            action="spot_buy",
            entry=1800.0,
            eth_qty=1.0,
            stop_loss=1700.0,
            take_profits=[1900.0],
            risk_reward=1.0,
            suggested_size=1.0,
            opened_at="2026-07-07T12:00:00Z",
            open_cycle_id="cycle_long",
            spot_price=1800.0,
        )
        equity, cash = paper.get_sizing_basis(1900.0)
        self.assertAlmostEqual(cash, 3200.0, places=2)
        self.assertAlmostEqual(equity, 5100.0, places=2)

    def test_compute_eth_qty_caps_at_max(self) -> None:
        with patch.object(config, "PAPER_PORTFOLIO_VALUE", 1_000_000.0):
            qty = validate.compute_eth_qty(1700.0, 1717.0, cash=1_000_000.0)
        self.assertAlmostEqual(qty, bot_config.MAX_ETH_QTY, places=4)

    def test_compute_eth_qty_raises_to_min_when_affordable(self) -> None:
        with patch.object(config, "PAPER_PORTFOLIO_VALUE", 1000.0):
            # 25% of a small portfolio is below MIN_ETH_QTY; bumps up when affordable.
            qty = validate.compute_eth_qty(1700.0, 1200.0, cash=5000.0)
        self.assertAlmostEqual(qty, bot_config.MIN_ETH_QTY, places=4)

    def test_open_uses_validated_size_not_llm_placeholder(self) -> None:
        suggestion = Suggestion(
            action="deriv_sell",
            size=0.99,
            entry=1576.0,
            stop_loss=1592.0,
            take_profits=[1545.0],
            risk_reward=2.0,
            rationale="wrong llm size",
        )
        validate.validate_trade_risk(suggestion, portfolio_value=5000.0)
        self.assertNotEqual(suggestion.size, 0.99)
        paper.update(suggestion, spot_price=1576.0, cycle_id="size_cycle")
        state = paper.get_state()
        self.assertAlmostEqual(state["eth_qty"], suggestion.size, places=4)

    def test_same_ob_add_does_not_widen_stop(self) -> None:
        first = Suggestion(
            action="deriv_sell",
            size=0.3,
            entry=1863.0,
            stop_loss=1878.0,
            take_profits=[1800.0],
            risk_reward=2.0,
            rationale="tranche 0.25",
            entry_tranche="0.25",
            order_block_ref="bearish:ob-a",
        )
        validate.validate_trade_risk(first, portfolio_value=5000.0)
        paper.update(first, spot_price=1863.0, cycle_id="wd_a1")

        second = Suggestion(
            action="deriv_sell",
            size=0.3,
            entry=1870.0,
            stop_loss=1885.0,  # wider stop — must not replace 1878
            take_profits=[1800.0],
            risk_reward=2.0,
            rationale="competing fill",
            entry_tranche="0.50",
            order_block_ref="bearish:ob-a",
        )
        validate.validate_trade_risk(second, portfolio_value=5000.0)
        paper.update(second, spot_price=1870.0, cycle_id="wd_a2")

        positions = paper.get_open_positions(1870.0)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["stop_loss"], 1878.0)
        self.assertIn("0.25", positions[0]["entry_tranches"])
        self.assertIn("0.50", positions[0]["entry_tranches"])
        self.assertGreater(positions[0]["eth_qty"], first.size)

    def test_different_ob_opens_separate_position(self) -> None:
        first = Suggestion(
            action="deriv_sell",
            size=0.3,
            entry=1863.0,
            stop_loss=1878.0,
            take_profits=[1800.0],
            risk_reward=2.0,
            rationale="ob a",
            entry_tranche="0.25",
            order_block_ref="bearish:ob-a",
        )
        validate.validate_trade_risk(first, portfolio_value=5000.0)
        paper.update(first, spot_price=1863.0, cycle_id="wd_a")

        second = Suggestion(
            action="deriv_sell",
            size=0.3,
            entry=1870.0,
            stop_loss=1885.0,
            take_profits=[1800.0],
            risk_reward=2.0,
            rationale="ob b",
            entry_tranche="0.25",
            order_block_ref="bearish:ob-b",
        )
        validate.validate_trade_risk(second, portfolio_value=5000.0)
        paper.update(second, spot_price=1870.0, cycle_id="wd_b")

        positions = paper.get_open_positions(1870.0)
        self.assertEqual(len(positions), 2)
        stops = {float(p["stop_loss"]) for p in positions}
        self.assertEqual(stops, {1878.0, 1885.0})
        refs = {p["order_block_ref"] for p in positions}
        self.assertEqual(refs, {"bearish:ob-a", "bearish:ob-b"})

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
