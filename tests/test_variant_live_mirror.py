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
        # One ETH contract (0.1) at a $170 stop risks $17 <= $18 cap -> taken
        clip = execute._variant_clip("ETH-USD", 2500.0, 2330.0)
        assert clip is not None
        self.assertAlmostEqual(clip[0], 0.1)
        self.assertAlmostEqual(clip[2], 17.0)

    def test_past_the_ceiling_is_skipped_not_silently_oversized(self) -> None:
        # One ETH contract at a $300 stop risks $30 > $18 cap
        self.assertIsNone(execute._variant_clip("ETH-USD", 2500.0, 2200.0))

    def test_the_2895_dollar_btc_swing_stop_is_now_a_skip(self) -> None:
        # Regression, mirror-pair study 2026-09-28: live#106 filled one BTC
        # nano at 86095 against an 83200 swing stop = $28.95 of risk on a
        # $12 plan (paper lost $10, live lost $28.95). Under the tightened
        # ceiling that clip is refused.
        self.assertIsNone(execute._variant_clip("BTC-USD", 86095.0, 83200.0))

    def test_ceiling_is_1_5x_the_risk_budget(self) -> None:
        self.assertAlmostEqual(
            bot_config.LIVE_VARIANT_MAX_RISK_USD,
            1.5 * bot_config.LIVE_VARIANT_RISK_USD,
        )


class TestExitFollow(TempDbTestCase):
    """Paper close flattens the live mirror (mirror-pair study 2026-09-28)."""

    def _mirror_trade(self, cycle_id: str) -> dict:
        return {"id": 77, "cycle_id": cycle_id, "source": "hq_day"}

    def test_paper_close_flattens_the_open_mirror(self) -> None:
        trade = self._mirror_trade("var_eva_day_47")
        with mock.patch.object(config, "EXECUTION_MODE", "live"), \
                mock.patch("live_ledger.get_open_trades",
                           side_effect=lambda source=None:
                           [trade] if source == "hq_day" else []), \
                mock.patch.object(execute, "close_live_trade",
                                  return_value={"ok": True}) as close:
            eva_variants._maybe_close_live_mirror("eva_day", 47, "time_exit")
        close.assert_called_once_with(77, reason="paper_time_exit")

    def test_no_open_mirror_is_a_noop(self) -> None:
        # The usual case for stop/target closes: live brackets already filled.
        with mock.patch.object(config, "EXECUTION_MODE", "live"), \
                mock.patch("live_ledger.get_open_trades", return_value=[]), \
                mock.patch.object(execute, "close_live_trade") as close:
            eva_variants._maybe_close_live_mirror("eva_day", 47, "stop")
        close.assert_not_called()

    def test_non_live_mode_never_touches_the_gateway(self) -> None:
        with mock.patch.object(config, "EXECUTION_MODE", "shadow"), \
                mock.patch.object(execute, "close_live_trade") as close:
            eva_variants._maybe_close_live_mirror("eva_day", 47, "time_exit")
        close.assert_not_called()

    def test_close_failure_cannot_break_the_paper_close(self) -> None:
        trade = self._mirror_trade("var_eva_day_47")
        with mock.patch.object(config, "EXECUTION_MODE", "live"), \
                mock.patch("live_ledger.get_open_trades",
                           side_effect=lambda source=None:
                           [trade] if source == "hq_day" else []), \
                mock.patch.object(execute, "close_live_trade",
                                  side_effect=RuntimeError("venue down")):
            # must not raise
            eva_variants._maybe_close_live_mirror("eva_day", 47, "time_exit")

    def test_mark_to_market_fires_the_exit_follow(self) -> None:
        pid = _open("eva_day", "m1_trigger")  # paper-only arm, no entry mirror
        bar = mock.Mock(ts=1_800_000_000, high=79000.0, low=78000.0,
                        close=78500.0)
        with mock.patch.object(eva_variants, "_m5_path", return_value=[bar]), \
                mock.patch.object(
                    eva_variants, "_maybe_close_live_mirror") as follow:
            closed = eva_variants.mark_to_market()
        self.assertEqual(closed, 1)
        follow.assert_called_once_with("eva_day", pid, "stop")


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
