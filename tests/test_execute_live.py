"""Tests for live execution sizing and kill switches (execute.py).

All tests run in EXECUTION_MODE=off/shadow — no gateway is ever contacted.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import execute
import live_ledger
from models import Suggestion


def _hq_suggestion(**overrides) -> Suggestion:
    base = dict(
        action="deriv_buy",
        size=0.5,
        entry=2000.0,
        stop_loss=1940.0,
        take_profits=[2060.0, 2120.0],
        risk_reward=2.0,
        rationale="test",
        product_id="ETH-USD",
        order_block_ref="ob-1",
    )
    base.update(overrides)
    return Suggestion(**base)


class ExecuteLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmpdir.name) / "test_ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "EXECUTION_MODE", "shadow"),
            # halt_live notifies ops (Telegram/email) — keep unit tests offline.
            patch.object(execute, "_notify_ops"),
            # Instrument resolution hits the products API — keep tests offline.
            patch.object(
                execute,
                "INSTRUMENT_MAP",
                {"ETH-USD": "ETP-20DEC30-CDE", "BTC-USD": "BIP-20DEC30-CDE"},
            ),
        ]
        for p in self._patches:
            p.start()
        live_ledger.init_db()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    # -- mode gating ----------------------------------------------------------

    def test_off_mode_is_noop(self) -> None:
        with patch.object(config, "EXECUTION_MODE", "off"):
            result = execute.maybe_execute_live(
                _hq_suggestion(), 2000.0, cycle_id="c1", source="hq"
            )
        self.assertIsNone(result)

    def test_no_trade_actions_skipped(self) -> None:
        result = execute.maybe_execute_live(
            _hq_suggestion(action="no_trade"), 2000.0, cycle_id="c1", source="hq"
        )
        self.assertIsNone(result)

    def test_missing_stop_loss_skipped(self) -> None:
        result = execute.maybe_execute_live(
            _hq_suggestion(stop_loss=None), 2000.0, cycle_id="c1", source="hq"
        )
        self.assertIsNone(result)

    # -- sizing ---------------------------------------------------------------

    def test_shadow_hq_sizing_is_half_sleeve(self) -> None:
        result = execute.maybe_execute_live(
            _hq_suggestion(entry=2000.0), 2000.0, cycle_id="c1", source="hq"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "shadow")
        # $2,000 sleeve × 50% = $1,000 notional → 0.5 ETH at $2,000.
        self.assertAlmostEqual(result["notional_usd"], 1000.0)
        self.assertAlmostEqual(result["qty"], 0.5)
        self.assertEqual(result["instrument"], "ETP-20DEC30-CDE")

    def test_live_qty_floor_skips_dust(self) -> None:
        # Mill $260 clip on BTC at $90k → 0.0029 BTC < 0.01 contract → skip.
        result = execute.maybe_execute_live(
            _hq_suggestion(product_id="BTC-USD", entry=90000.0, stop_loss=88000.0),
            90000.0,
            cycle_id="c1",
            source="mill",
        )
        self.assertIsNone(result)

    def test_live_floor_below_paper_min(self) -> None:
        # The paper ETH min (0.25 ≈ $1,100 at ETH>$4,400) would block a $1,000
        # live clip; live floors must be lower and independent.
        self.assertLess(
            bot_config.LIVE_PRODUCT_QTY_FLOORS["ETH-USD"],
            bot_config.PRODUCT_QTY_CAPS["ETH-USD"][0],
        )

    def test_paper_caps_untouched_by_live_config(self) -> None:
        self.assertEqual(bot_config.TRADE_DEPLOY_PCT, 0.25)
        self.assertEqual(bot_config.PRODUCT_QTY_CAPS["ETH-USD"][0], 0.25)

    # -- kill switches --------------------------------------------------------

    def test_scale_in_tranche_is_paper_only(self) -> None:
        result = execute.maybe_execute_live(
            _hq_suggestion(entry_tranche=str(bot_config.ADD_FIB_LEVEL)),
            2000.0,
            cycle_id="c1",
            source="hq",
        )
        self.assertIsNone(result)

    def test_max_open_hq_positions(self) -> None:
        for i in range(bot_config.LIVE_MAX_OPEN_HQ):
            live_ledger.record_open(
                cycle_id=f"c{i}",
                source="hq",
                product_id="ETH-USD",
                instrument="ETH_USDC-PERPETUAL",
                side="long",
                qty=0.5,
                entry=2000.0,
                stop_loss=1940.0,
                take_profits_json="[]",
                order_id=None,
                stop_order_id=None,
                notes=f"ob:ob-{i}",
            )
        result = execute.maybe_execute_live(
            _hq_suggestion(order_block_ref="ob-new"),
            2000.0,
            cycle_id="c9",
            source="hq",
        )
        self.assertIsNone(result)

    def test_same_order_block_not_taken_twice(self) -> None:
        live_ledger.record_open(
            cycle_id="c1",
            source="hq",
            product_id="ETH-USD",
            instrument="ETH_USDC-PERPETUAL",
            side="long",
            qty=0.5,
            entry=2000.0,
            stop_loss=1940.0,
            take_profits_json="[]",
            order_id=None,
            stop_order_id=None,
            notes="ob:ob-1",
        )
        result = execute.maybe_execute_live(
            _hq_suggestion(order_block_ref="ob-1"),
            2000.0,
            cycle_id="c2",
            source="hq",
        )
        self.assertIsNone(result)

    def test_daily_loss_halts_sleeve(self) -> None:
        trade_id = live_ledger.record_open(
            cycle_id="c1",
            source="hq",
            product_id="ETH-USD",
            instrument="ETH_USDC-PERPETUAL",
            side="long",
            qty=0.5,
            entry=2000.0,
            stop_loss=1940.0,
            take_profits_json="[]",
            order_id=None,
            stop_order_id=None,
            notes=None,
        )
        live_ledger.record_close(
            trade_id,
            exit_price=1940.0,
            pnl_usd=-bot_config.LIVE_DAILY_LOSS_LIMIT_USD,
            close_reason="stop",
        )
        result = execute.maybe_execute_live(
            _hq_suggestion(order_block_ref="ob-2"),
            2000.0,
            cycle_id="c2",
            source="hq",
        )
        self.assertIsNone(result)
        self.assertIsNotNone(execute.is_halted())
        self.assertTrue(execute.is_halted().startswith("daily_loss:hq"))

    def test_exposure_cap_1x(self) -> None:
        # One open $1,600 position; a new $1,000 clip would exceed $2,000 × 1x.
        live_ledger.record_open(
            cycle_id="c1",
            source="hq",
            product_id="ETH-USD",
            instrument="ETH_USDC-PERPETUAL",
            side="long",
            qty=0.8,
            entry=2000.0,
            stop_loss=1940.0,
            take_profits_json="[]",
            order_id=None,
            stop_order_id=None,
            notes="ob:ob-0",
        )
        result = execute.maybe_execute_live(
            _hq_suggestion(order_block_ref="ob-2"),
            2000.0,
            cycle_id="c2",
            source="hq",
        )
        self.assertIsNone(result)

    def test_mill_daily_fill_cap_off_by_default(self) -> None:
        self.assertEqual(bot_config.LIVE_MILL_MAX_FILLS_PER_DAY, 0)
        today = execute._today()
        live_ledger.set_meta("mill_fills_date", f"{today}:9")
        result = execute.maybe_execute_live(
            _hq_suggestion(order_block_ref="ob-m"),
            2000.0,
            cycle_id="c1",
            source="mill",
        )
        self.assertIsNotNone(result)

    def test_mill_daily_fill_cap_when_enabled(self) -> None:
        today = execute._today()
        live_ledger.set_meta("mill_fills_date", f"{today}:2")
        with patch.object(bot_config, "LIVE_MILL_MAX_FILLS_PER_DAY", 2):
            result = execute.maybe_execute_live(
                _hq_suggestion(order_block_ref="ob-m"),
                2000.0,
                cycle_id="c1",
                source="mill",
            )
        self.assertIsNone(result)

    def test_mill_shadow_sizing(self) -> None:
        # Mill $260 clip → 0.13 ETH at $2,000, above the 0.1 contract floor.
        result = execute.maybe_execute_live(
            _hq_suggestion(entry=2000.0), 2000.0, cycle_id="c1", source="mill"
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["notional_usd"], 260.0)
        self.assertAlmostEqual(result["qty"], 0.13)

    def test_halt_blocks_and_daily_halt_expires(self) -> None:
        execute.halt_live("daily_loss:hq:-200.00")
        result = execute.maybe_execute_live(
            _hq_suggestion(), 2000.0, cycle_id="c1", source="hq"
        )
        self.assertIsNone(result)
        # Simulate the next UTC day: daily halts clear, manual ones persist.
        live_ledger.set_meta("live_halt_date", "2000-01-01")
        self.assertIsNone(execute.is_halted())
        execute.halt_live("stop_reject:ETH_USDC-PERPETUAL:boom")
        live_ledger.set_meta("live_halt_date", "2000-01-01")
        self.assertIsNotNone(execute.is_halted())


if __name__ == "__main__":
    unittest.main()
