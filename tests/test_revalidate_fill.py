"""Accept-time revalidation.

An idea is priced when it is minted and filled whenever someone taps Accept,
which can be minutes later. These tests pin what happens to the plan in that
gap: structural levels hold, dead setups are refused, and targets the market
already took are dropped instead of firing a market order on arming.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_config  # noqa: E402
import execute as live_exec  # noqa: E402
import trade_ideas_bridge as bridge  # noqa: E402

LONG = {"direction": "long", "entry": 2411.5, "stop_loss": 2385.0,
        "take_profits": [2440.0, 2477.0, 2534.0]}
SHORT = {"direction": "short", "entry": 2411.5, "stop_loss": 2440.0,
         "take_profits": [2385.0, 2350.0]}


class RevalidateLongTests(unittest.TestCase):
    # Planned risk is 2411.50 - 2385.00 = 26.50, so the chase limit is 13.25.

    def test_unmoved_market_keeps_the_whole_plan(self) -> None:
        plan = live_exec.revalidate_levels(**LONG, spot=2411.5)
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["take_profits"], [2440.0, 2477.0, 2534.0])
        self.assertEqual(plan["entry"], 2411.5)
        self.assertEqual(plan["stop_loss"], 2385.0)
        self.assertFalse(plan["shifted"])

    def test_a_worse_entry_shifts_the_structure_so_risk_is_unchanged(self) -> None:
        plan = live_exec.revalidate_levels(**LONG, spot=2420.0)
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["shifted"])
        self.assertEqual(plan["entry"], 2420.0)
        self.assertEqual(plan["stop_loss"], 2393.5)
        self.assertEqual(plan["take_profits"], [2448.5, 2485.5, 2542.5])
        # Same distance to the stop as the plan called for.
        self.assertAlmostEqual(plan["entry"] - plan["stop_loss"], 26.5)

    def test_a_better_entry_keeps_the_structural_levels_and_risks_less(self) -> None:
        plan = live_exec.revalidate_levels(**LONG, spot=2400.0)
        self.assertTrue(plan["ok"])
        self.assertFalse(plan["shifted"])
        self.assertEqual(plan["stop_loss"], 2385.0)
        self.assertEqual(plan["take_profits"], [2440.0, 2477.0, 2534.0])
        self.assertAlmostEqual(plan["entry"] - plan["stop_loss"], 15.0)

    def test_running_past_the_entry_is_refused_as_chasing(self) -> None:
        """Eva's case: 38 points past entry is not the trade that was posted."""
        plan = live_exec.revalidate_levels(**LONG, spot=2450.0)
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "chased")
        self.assertAlmostEqual(plan["chase_r"], 1.45, places=2)

    def test_chase_limit_is_measured_in_units_of_planned_risk(self) -> None:
        self.assertTrue(live_exec.revalidate_levels(**LONG, spot=2424.0)["ok"])
        self.assertFalse(live_exec.revalidate_levels(**LONG, spot=2425.5)["ok"])

    def test_price_through_the_stop_is_refused(self) -> None:
        plan = live_exec.revalidate_levels(**LONG, spot=2380.0)
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "stop_breached")

    def test_collapsed_reward_to_risk_is_refused(self) -> None:
        """A backstop for ideas that were poor before any drift."""
        plan = live_exec.revalidate_levels(
            direction="long", entry=2411.5, stop_loss=2385.0,
            take_profits=[2420.0], spot=2411.5,
        )
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "rr_collapsed")

    def test_reward_is_judged_on_the_whole_ladder_not_just_tp1(self) -> None:
        """A scale-out ladder puts TP1 close in on purpose; that is not a flaw."""
        plan = live_exec.revalidate_levels(**LONG, spot=2411.5)
        self.assertTrue(plan["ok"])
        self.assertGreater(plan["risk_reward"], 2.0)

    def test_inconsistent_levels_are_refused(self) -> None:
        plan = live_exec.revalidate_levels(
            direction="long", entry=2400.0, stop_loss=2450.0,
            take_profits=[2500.0], spot=2400.0,
        )
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "bad_levels")

    def test_missing_mark_is_refused_rather_than_guessed(self) -> None:
        plan = live_exec.revalidate_levels(**LONG, spot=0.0)
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "no_mark")


class RevalidateShortTests(unittest.TestCase):
    # Planned risk is 2440.00 - 2411.50 = 28.50, so the chase limit is 14.25.

    def test_a_better_short_entry_keeps_the_structural_levels(self) -> None:
        plan = live_exec.revalidate_levels(**SHORT, spot=2420.0)
        self.assertTrue(plan["ok"])
        self.assertFalse(plan["shifted"])
        self.assertEqual(plan["stop_loss"], 2440.0)
        self.assertEqual(plan["take_profits"], [2385.0, 2350.0])

    def test_a_worse_short_entry_shifts_the_structure_down(self) -> None:
        plan = live_exec.revalidate_levels(**SHORT, spot=2400.0)
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["shifted"])
        self.assertEqual(plan["stop_loss"], 2428.5)
        self.assertEqual(plan["take_profits"], [2373.5, 2338.5])
        self.assertAlmostEqual(plan["stop_loss"] - plan["entry"], 28.5)

    def test_short_through_its_stop_is_refused(self) -> None:
        plan = live_exec.revalidate_levels(**SHORT, spot=2445.0)
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "stop_breached")

    def test_short_that_already_ran_down_is_refused_as_chasing(self) -> None:
        plan = live_exec.revalidate_levels(**SHORT, spot=2380.0)
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "chased")

    def test_short_targets_are_ordered_downward(self) -> None:
        plan = live_exec.revalidate_levels(
            direction="short", entry=2411.5, stop_loss=2440.0,
            take_profits=[2350.0, 2385.0], spot=2411.5,
        )
        self.assertEqual(plan["take_profits"], [2385.0, 2350.0])


class RevalidatedPlanTests(unittest.TestCase):
    def _gw(self, mark: float) -> MagicMock:
        gw = MagicMock()
        gw.resolve_instrument.return_value = "ETP-20DEC30-CDE"
        gw.get_ticker.return_value = {"mark_price": mark}
        return gw

    def test_uses_the_live_mark(self) -> None:
        with patch.object(live_exec, "get_gateway", return_value=self._gw(2420.0)):
            plan = live_exec._revalidated_plan(product_id="ETH-USD", **LONG)
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["spot"], 2420.0)
        self.assertEqual(plan["stop_loss"], 2393.5)

    def test_a_mark_that_has_run_away_refuses_the_fill(self) -> None:
        with patch.object(live_exec, "get_gateway", return_value=self._gw(2450.0)):
            plan = live_exec._revalidated_plan(product_id="ETH-USD", **LONG)
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["reason"], "chased")

    def test_unreadable_ticker_falls_open_to_the_minted_plan(self) -> None:
        """A ticker outage must not become a trading outage."""
        gw = MagicMock()
        gw.resolve_instrument.side_effect = RuntimeError("api down")
        with patch.object(live_exec, "get_gateway", return_value=gw):
            plan = live_exec._revalidated_plan(product_id="ETH-USD", **LONG)
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["no_mark"])
        self.assertEqual(plan["take_profits"], LONG["take_profits"])

    def test_a_zero_mark_falls_open_too(self) -> None:
        """An empty ticker is a missing price, not a price of zero."""
        with patch.object(live_exec, "get_gateway", return_value=self._gw(0.0)):
            plan = live_exec._revalidated_plan(product_id="ETH-USD", **LONG)
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["no_mark"])

    def test_disabled_flag_passes_the_plan_through(self) -> None:
        with patch.object(bot_config, "LIVE_REVALIDATE_ON_FILL", False):
            plan = live_exec._revalidated_plan(product_id="ETH-USD", **LONG)
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["skipped"])


class MillGateTests(unittest.TestCase):
    """The gate is shared, so an Accept and an auto fill are checked alike."""

    def _capacity(self, **kw):
        base = {"open": 0, "max_open": 3, "slots_free": 3, "halted": None,
                "open_notional_usd": 0.0, "sleeve_usd": 1400.0,
                "sleeve_free_usd": 1400.0, "open_trades": []}
        base.update(kw)
        return base

    def test_stale_idea_is_refused_before_any_order_is_placed(self) -> None:
        with patch.object(live_exec, "mill_capacity", return_value=self._capacity()), \
             patch.object(live_exec, "_revalidated_plan",
                          return_value={"ok": False, "reason": "stop_breached",
                                        "spot": 2380.0}), \
             patch.object(live_exec, "maybe_execute_live") as ex:
            verdict = live_exec.execute_mill_idea(
                idea_id=42, product_id="ETH-USD", direction="long",
                entry=2411.5, stop_loss=2385.0, take_profits=[2440.0],
                confidence=0.7, fill_type="manual", accepted_by=8282981740,
            )
        self.assertFalse(verdict["executed"])
        self.assertEqual(verdict["skip_reason"], "stop_breached")
        ex.assert_not_called()

    def test_fill_uses_the_revalidated_levels(self) -> None:
        with patch.object(live_exec, "mill_capacity", return_value=self._capacity()), \
             patch.object(live_exec, "_revalidated_plan",
                          return_value={"ok": True, "entry": 2420.0,
                                        "stop_loss": 2393.5,
                                        "take_profits": [2448.5, 2485.5],
                                        "risk_reward": 2.4, "spot": 2420.0}), \
             patch.object(live_exec, "maybe_execute_live",
                          return_value={"mode": "live"}) as ex:
            verdict = live_exec.execute_mill_idea(
                idea_id=42, product_id="ETH-USD", direction="long",
                entry=2411.5, stop_loss=2385.0,
                take_profits=[2440.0, 2477.0, 2534.0],
                confidence=0.7, fill_type="manual", accepted_by=8282981740,
            )
        self.assertTrue(verdict["executed"])
        suggestion = ex.call_args.args[0]
        self.assertEqual(suggestion.take_profits, [2448.5, 2485.5])
        self.assertEqual(suggestion.entry, 2420.0)
        self.assertEqual(ex.call_args.args[1], 2420.0)
        self.assertEqual(suggestion.stop_loss, 2393.5)

    def test_the_auto_path_is_revalidated_too(self) -> None:
        with patch.object(live_exec, "mill_capacity", return_value=self._capacity()), \
             patch.object(live_exec, "_revalidated_plan",
                          return_value={"ok": False, "reason": "chased"}), \
             patch.object(live_exec, "maybe_execute_live") as ex:
            verdict = live_exec.execute_mill_idea(
                idea_id=42, product_id="ETH-USD", direction="long",
                entry=2411.5, stop_loss=2385.0, take_profits=[2440.0],
                confidence=0.9, fill_type="auto",
            )
        self.assertEqual(verdict["skip_reason"], "chased")
        ex.assert_not_called()


class StaleReplyTests(unittest.TestCase):
    def test_operator_is_told_why_the_accept_did_not_fill(self) -> None:
        reply = bridge.format_manual_fill_reply(
            {"executed": False, "skip_reason": "stop_breached",
             "revalidation": {"spot": 2380.0}, "capacity": {}},
            42,
        )
        self.assertIn("market moved", reply)
        self.assertIn("through the stop", reply)
        self.assertIn("2,380", reply)

    def test_every_stale_reason_has_wording(self) -> None:
        for reason in bridge._STALE_REASONS:
            reply = bridge.format_manual_fill_reply(
                {"executed": False, "skip_reason": reason,
                 "revalidation": {}, "capacity": {}},
                7,
            )
            self.assertTrue(reply and "NOT filled" in reply, reason)


if __name__ == "__main__":
    unittest.main()
