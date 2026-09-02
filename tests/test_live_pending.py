"""Live waits for its entry instead of buying the mark.

The behaviour these pin is the one measured in analysis/run_fill_study.py: a
plan whose entry has not traded is held, not chased, and it is retired by the
next read of the chart rather than by a clock.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import execute
import live_pending
import vault
from models import Suggestion


def _sug(
    action: str = "deriv_buy",
    entry: float = 2400.0,
    stop: float = 2350.0,
    product_id: str = "ETH-USD",
) -> Suggestion:
    return Suggestion(
        action=action,
        size=100.0,
        entry=entry,
        stop_loss=stop,
        take_profits=[2440.0, 2470.0, 2500.0],
        risk_reward=1.8,
        rationale="test plan",
        order_block_ref="ob-1",
        product_id=product_id,
    )


class LivePendingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "EXECUTION_MODE", "shadow"),
            patch.object(bot_config, "LIVE_PENDING_ENTRIES_ENABLED", True),
            patch.object(bot_config, "LIVE_PENDING_EXPIRY_HOURS", 4.0),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self._db = db
        live_pending.init_db()

    def _backdate(self, product_id: str, hours: float) -> None:
        when = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn = sqlite3.connect(self._db)
        conn.execute(
            "UPDATE live_pending SET created_at = ? WHERE product_id = ?",
            (when, product_id),
        )
        conn.commit()
        conn.close()

    # --- what counts as reachable -------------------------------------

    def test_a_long_limit_below_the_mark_is_not_yet_fillable(self) -> None:
        self.assertFalse(live_pending.is_fillable("long", 2400.0, 2420.0))

    def test_a_long_limit_at_or_through_the_mark_is_fillable(self) -> None:
        self.assertTrue(live_pending.is_fillable("long", 2400.0, 2400.0))
        self.assertTrue(live_pending.is_fillable("long", 2400.0, 2390.0))

    def test_a_short_limit_above_the_mark_is_not_yet_fillable(self) -> None:
        self.assertFalse(live_pending.is_fillable("short", 2400.0, 2380.0))
        self.assertTrue(live_pending.is_fillable("short", 2400.0, 2410.0))

    def test_a_plan_without_an_entry_never_parks_silently(self) -> None:
        self.assertTrue(live_pending.is_fillable("long", None, 2420.0))

    # --- holding and releasing ----------------------------------------

    def test_a_plan_price_has_not_reached_is_held(self) -> None:
        live_pending.record(_sug(entry=2400.0), cycle_id="c1")
        rows = live_pending.get_pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_id"], "ETH-USD")
        self.assertEqual(rows[0]["side"], "long")
        self.assertAlmostEqual(rows[0]["entry"], 2400.0)

    def test_a_waiting_plan_fires_when_price_trades_to_it(self) -> None:
        live_pending.record(_sug(entry=2400.0), cycle_id="c1")
        with patch.object(execute, "maybe_execute_live") as sent:
            sent.return_value = {"mode": "shadow"}
            live_pending.sweep({"ETH-USD": 2399.0})
        self.assertEqual(sent.call_count, 1)
        sug = sent.call_args.args[0]
        self.assertEqual(sug.action, "deriv_buy")
        self.assertAlmostEqual(sug.entry, 2400.0)
        self.assertAlmostEqual(sug.stop_loss, 2350.0)
        self.assertEqual(sug.take_profits, [2440.0, 2470.0, 2500.0])
        self.assertEqual(sent.call_args.kwargs["cycle_id"], "c1")
        self.assertEqual(live_pending.get_pending(), [])

    def test_a_waiting_plan_price_never_reaches_stays_put(self) -> None:
        live_pending.record(_sug(entry=2400.0), cycle_id="c1")
        with patch.object(execute, "maybe_execute_live") as sent:
            live_pending.sweep({"ETH-USD": 2425.0})
        sent.assert_not_called()
        self.assertEqual(len(live_pending.get_pending()), 1)

    def test_a_touched_plan_is_dropped_even_if_the_order_is_refused(self) -> None:
        """The entry traded once. If a halt stopped us, that moment is gone —
        holding the plan would only fill it later at a worse price."""
        live_pending.record(_sug(entry=2400.0), cycle_id="c1")
        with patch.object(execute, "maybe_execute_live", return_value=None):
            live_pending.sweep({"ETH-USD": 2399.0})
        self.assertEqual(live_pending.get_pending(), [])

    def test_a_mark_already_through_the_stop_is_not_bought(self) -> None:
        live_pending.record(_sug(entry=2400.0, stop=2350.0), cycle_id="c1")
        with patch.object(execute, "maybe_execute_live") as sent:
            live_pending.sweep({"ETH-USD": 2340.0})
        sent.assert_not_called()
        self.assertEqual(live_pending.get_pending(), [])

    def test_a_short_plan_fires_when_price_rises_to_it(self) -> None:
        live_pending.record(
            _sug(action="deriv_sell", entry=2500.0, stop=2540.0), cycle_id="c1"
        )
        with patch.object(execute, "maybe_execute_live") as sent:
            sent.return_value = {"mode": "shadow"}
            live_pending.sweep({"ETH-USD": 2505.0})
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(sent.call_args.args[0].action, "deriv_sell")

    # --- what retires a plan ------------------------------------------

    def test_a_decline_cancels_the_plan_still_waiting(self) -> None:
        live_pending.record(_sug(), cycle_id="c1")
        live_pending.cancel("ETH-USD")
        self.assertEqual(live_pending.get_pending(), [])

    def test_a_decline_on_one_product_leaves_the_other_waiting(self) -> None:
        live_pending.record(_sug(product_id="ETH-USD"), cycle_id="c1")
        live_pending.record(
            _sug(product_id="BTC-USD", entry=78000.0, stop=77000.0), cycle_id="c2"
        )
        live_pending.cancel("ETH-USD")
        rows = live_pending.get_pending()
        self.assertEqual([r["product_id"] for r in rows], ["BTC-USD"])

    def test_a_fresh_plan_supersedes_the_one_still_waiting(self) -> None:
        live_pending.record(_sug(entry=2400.0), cycle_id="c1")
        live_pending.record(_sug(entry=2375.0), cycle_id="c2")
        rows = live_pending.get_pending()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["entry"], 2375.0)
        self.assertEqual(rows[0]["cycle_id"], "c2")

    def test_a_stale_plan_expires_rather_than_filling_days_later(self) -> None:
        live_pending.record(_sug(entry=2400.0), cycle_id="c1")
        self._backdate("ETH-USD", hours=5.0)
        with patch.object(execute, "maybe_execute_live") as sent:
            live_pending.sweep({"ETH-USD": 2425.0})
        sent.assert_not_called()
        self.assertEqual(live_pending.get_pending(), [])

    def test_a_fresh_plan_restarts_the_expiry_clock(self) -> None:
        live_pending.record(_sug(entry=2400.0), cycle_id="c1")
        self._backdate("ETH-USD", hours=5.0)
        live_pending.record(_sug(entry=2390.0), cycle_id="c2")
        with patch.object(execute, "maybe_execute_live") as sent:
            live_pending.sweep({"ETH-USD": 2425.0})
        sent.assert_not_called()
        self.assertEqual(len(live_pending.get_pending()), 1)

    # --- guards --------------------------------------------------------

    def test_execution_off_sweeps_nothing(self) -> None:
        live_pending.record(_sug(entry=2400.0), cycle_id="c1")
        with patch.object(config, "EXECUTION_MODE", "off"):
            with patch.object(execute, "maybe_execute_live") as sent:
                live_pending.sweep({"ETH-USD": 2399.0})
        sent.assert_not_called()
        self.assertEqual(len(live_pending.get_pending()), 1)

    def test_a_missing_spot_leaves_the_plan_alone(self) -> None:
        live_pending.record(_sug(entry=2400.0), cycle_id="c1")
        with patch.object(execute, "maybe_execute_live") as sent:
            live_pending.sweep({})
        sent.assert_not_called()
        self.assertEqual(len(live_pending.get_pending()), 1)

    def test_a_no_trade_is_never_parked(self) -> None:
        self.assertIsNone(
            live_pending.record(Suggestion.no_trade("nothing here"), cycle_id="c1")
        )
        self.assertEqual(live_pending.get_pending(), [])


class PendingSizingTests(unittest.TestCase):
    """A waiting clip is sized against the price it will actually fill at."""

    def test_the_clip_is_sized_against_the_entry_not_the_mark(self) -> None:
        sug = _sug(entry=2400.0, stop=2350.0)
        with patch.object(bot_config, "LIVE_HQ_RISK_PCT", 0.005):
            at_entry = vault.propose(sug, spot=2400.0, open_rows=[])
            at_mark = vault.propose(sug, spot=2420.0, open_rows=[])

        # $10 budget: 50 points of risk buys two ETH nanos, 70 points buys one.
        self.assertTrue(at_entry["admitted"])
        self.assertTrue(at_mark["admitted"])
        self.assertAlmostEqual(at_entry["qty"], 0.2)
        self.assertAlmostEqual(at_mark["qty"], 0.1)

        # A pending fills at 2400, so 50 points is the risk either clip really
        # takes. Sizing off the mark prices a gap the fill will not pay, and
        # spends half the budget it was allowed.
        realized_at_entry = abs(2400.0 - 2350.0) * at_entry["qty"]
        realized_at_mark = abs(2400.0 - 2350.0) * at_mark["qty"]
        self.assertAlmostEqual(realized_at_entry, 10.0)
        self.assertAlmostEqual(realized_at_mark, 5.0)


if __name__ == "__main__":
    unittest.main()
