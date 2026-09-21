"""Live mirrors for eva_swing_llm / eva_day (prereg amendment 2026-09-21).

The properties that must hold: the paper book is written before and regardless
of the mirror; m1_trigger can never reach live; the chase guard keeps the
mirror honest; sizing is fixed-risk with a bounded contract-floor overshoot;
and the family stacking cap sees control + both mirrors as one sleeve.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot_config
import config
import eva_variants
import execute
from models import Suggestion


class TempDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = config.LEDGER_DB
        config.LEDGER_DB = Path(self._tmp.name) / "test_ledger.db"
        eva_variants.init_db()

    def tearDown(self) -> None:
        config.LEDGER_DB = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


def _open(variant, entry_source, **kw):
    args = dict(
        product_id="BTC-USD", side="long", entry=80000.0, stop_loss=79200.0,
        take_profits=[81000.0], entry_source=entry_source, cycle_id="c1",
    )
    args.update(kw)
    return eva_variants.open_position(variant, **args)


class TestMirrorGating(TempDbTestCase):
    def test_swing_vision_mirrors_with_source_tag(self) -> None:
        with mock.patch.object(bot_config, "EVA_SWING_LLM_LIVE_ENABLED", True), \
                mock.patch("research.get_spot_price", return_value=80000.0), \
                mock.patch.object(execute, "maybe_execute_live") as live:
            pid = _open("eva_swing_llm", "swing_vision")
        self.assertIsNotNone(pid)
        self.assertEqual(live.call_count, 1)
        self.assertEqual(live.call_args.kwargs["source"], "hq_swing")
        self.assertEqual(live.call_args.kwargs["cycle_id"],
                         f"var_eva_swing_llm_{pid}")

    def test_day_rebracket_mirrors_and_m1_never_does(self) -> None:
        with mock.patch.object(bot_config, "EVA_DAY_LIVE_ENABLED", True), \
                mock.patch("research.get_spot_price", return_value=80000.0), \
                mock.patch.object(execute, "maybe_execute_live") as live:
            _open("eva_day", "vision_rebracket")
            _open("eva_day", "m1_trigger", side="short",
                  stop_loss=80800.0, take_profits=[79000.0])
        sources = [c.kwargs["source"] for c in live.call_args_list]
        self.assertEqual(sources, ["hq_day"])   # exactly one, never m1

    def test_flag_off_is_a_kill_switch(self) -> None:
        with mock.patch.object(bot_config, "EVA_DAY_LIVE_ENABLED", False), \
                mock.patch.object(execute, "maybe_execute_live") as live:
            pid = _open("eva_day", "vision_rebracket")
        self.assertIsNotNone(pid)      # paper book untouched by the flag
        live.assert_not_called()

    def test_other_books_never_mirror(self) -> None:
        with mock.patch.object(execute, "maybe_execute_live") as live:
            _open("eva_swing_mech", "vision_mirror")
            _open("eva_geom", "vision_mirror")
        live.assert_not_called()

    def test_chase_guard_skips_live_but_keeps_paper(self) -> None:
        with mock.patch.object(bot_config, "EVA_DAY_LIVE_ENABLED", True), \
                mock.patch("research.get_spot_price",
                           return_value=80000.0 * 1.01), \
                mock.patch.object(execute, "maybe_execute_live") as live:
            pid = _open("eva_day", "vision_rebracket")
        self.assertIsNotNone(pid)
        live.assert_not_called()

    def test_mirror_failure_cannot_break_the_open(self) -> None:
        """The experiment writes first; the mirror is bookkeeping after it."""
        with mock.patch.object(bot_config, "EVA_DAY_LIVE_ENABLED", True), \
                mock.patch("research.get_spot_price",
                           side_effect=RuntimeError("feed down")):
            pid = _open("eva_day", "vision_rebracket")
        self.assertIsNotNone(pid)
        self.assertEqual(len(eva_variants.open_positions("eva_day")), 1)


class TestVariantClip(unittest.TestCase):
    def test_fixed_risk_rounds_down_to_contract_multiples(self) -> None:
        # $12 over an $800 stop = 0.015 BTC -> floors to 0.01 (one contract)
        clip = execute._variant_clip("BTC-USD", 80000.0, 79200.0)
        assert clip is not None
        qty, notional, risk = clip
        self.assertAlmostEqual(qty, 0.01)
        self.assertAlmostEqual(risk, 8.0)      # bounded UNDER budget

    def test_tight_stop_buys_more_contracts(self) -> None:
        # $12 over a $150 stop = 0.08 BTC -> 8 contracts
        clip = execute._variant_clip("BTC-USD", 80000.0, 79850.0)
        assert clip is not None
        self.assertAlmostEqual(clip[0], 0.08)

    def test_wide_stop_takes_one_contract_up_to_the_ceiling(self) -> None:
        # One ETH contract (0.1) at a $300 stop risks $30 <= $40 cap -> taken
        clip = execute._variant_clip("ETH-USD", 2500.0, 2200.0)
        assert clip is not None
        self.assertAlmostEqual(clip[0], 0.1)
        self.assertAlmostEqual(clip[2], 30.0)

    def test_past_the_ceiling_is_skipped_not_silently_oversized(self) -> None:
        # One ETH contract at a $500 stop risks $50 > $40 cap
        self.assertIsNone(execute._variant_clip("ETH-USD", 2500.0, 2000.0))


class TestFamilyCaps(unittest.TestCase):
    def _trade(self, source, side="long", entry=80000.0, stop=79600.0, qty=0.01):
        return {"product_id": "BTC-USD", "side": side, "entry": entry,
                "stop_loss": stop, "initial_stop_loss": stop, "qty": qty,
                "qty_open": qty}

    def test_stack_risk_sums_across_the_family(self) -> None:
        def fake_open(source=None):
            return [self._trade(source)] if source in ("hq", "hq_day") else []

        with mock.patch.object(execute.live_ledger, "get_open_trades",
                               side_effect=fake_open):
            risk = execute._hq_family_open_risk("BTC-USD", "long")
        self.assertAlmostEqual(risk, 8.0)      # two books x $4 each

    def test_opposite_side_does_not_count_against_the_stack(self) -> None:
        def fake_open(source=None):
            return [self._trade(source, side="short")] if source == "hq" else []

        with mock.patch.object(execute.live_ledger, "get_open_trades",
                               side_effect=fake_open):
            self.assertEqual(
                execute._hq_family_open_risk("BTC-USD", "long"), 0.0)

    def test_execute_variant_branch_respects_stack_cap_in_shadow(self) -> None:
        sugg = Suggestion(
            action="spot_buy", size=0.0, entry=80000.0, stop_loss=79200.0,
            take_profits=[81000.0], product_id="BTC-USD",
        )

        def crowded(source=None):
            # $44 of family risk already open on this product+side
            return ([self._trade(source, stop=78900.0, qty=0.04)]
                    if source == "hq" else [])

        with mock.patch.object(execute.config, "EXECUTION_MODE", "shadow"), \
                mock.patch.object(execute, "is_halted", return_value=None), \
                mock.patch.object(execute, "_realized_pnl_today",
                                  return_value=0.0), \
                mock.patch.object(execute.live_ledger, "get_open_trades",
                                  side_effect=crowded):
            result = execute.maybe_execute_live(
                sugg, 80000.0, cycle_id="var_t_1", source="hq_day")
        self.assertIsNone(result)

        with mock.patch.object(execute.config, "EXECUTION_MODE", "shadow"), \
                mock.patch.object(execute, "is_halted", return_value=None), \
                mock.patch.object(execute, "_realized_pnl_today",
                                  return_value=0.0), \
                mock.patch.object(execute.live_ledger, "get_open_trades",
                                  return_value=[]):
            result = execute.maybe_execute_live(
                sugg, 80000.0, cycle_id="var_t_2", source="hq_day")
        assert result is not None
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["source"], "hq_day")
        self.assertAlmostEqual(result["qty"], 0.01)

    def test_daily_loss_pools_the_whole_family(self) -> None:
        """Three books, one sleeve, one halt — not three private budgets."""
        losses = {"hq": -70.0, "hq_swing": -50.0, "hq_day": -45.0}
        with mock.patch.object(execute, "_realized_pnl_today",
                               side_effect=lambda s: losses.get(s, 0.0)), \
                mock.patch.object(execute, "halt_live") as halt:
            ok = execute._check_daily_loss("hq_day")
        self.assertFalse(ok)               # -165 pooled > -160 limit
        halt.assert_called_once()

    def test_family_members_under_the_pool_still_trade(self) -> None:
        losses = {"hq": -70.0, "hq_swing": -50.0, "hq_day": 0.0}
        with mock.patch.object(execute, "_realized_pnl_today",
                               side_effect=lambda s: losses.get(s, 0.0)), \
                mock.patch.object(execute, "halt_live") as halt:
            ok = execute._check_daily_loss("hq_swing")
        self.assertTrue(ok)                # -120 pooled, under the limit
        halt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
