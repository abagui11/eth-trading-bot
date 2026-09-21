"""Tests for live execution sizing and kill switches (execute.py).

All tests run in EXECUTION_MODE=off/shadow — no gateway is ever contacted.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
            patch.object(bot_config, "CASE_STUDY_ENABLED", False),
            # halt_live notifies ops (Telegram/email) — keep unit tests offline.
            patch.object(execute, "_notify_ops"),
            # Instrument resolution hits the products API — keep tests offline.
            patch.object(
                execute,
                "INSTRUMENT_MAP",
                {"ETH-USD": "ETP-20DEC30-CDE", "BTC-USD": "BIP-20DEC30-CDE"},
            ),
            # Nothing in a unit test may reach Coinbase. Raising here means a
            # gateway call added to this path later fails loudly offline
            # instead of quietly hitting the live venue.
            patch.object(
                execute, "get_gateway", side_effect=RuntimeError("offline test")
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

    def test_shadow_hq_clip_is_sized_to_the_risk_budget(self) -> None:
        # Budget pinned so the clip stays legible: a 60-point stop risks $6 a
        # nano, so exactly one fits $10. $200 notional is what that costs at
        # $2,000, not a notional target the qty was solved backwards from.
        with patch.object(bot_config, "LIVE_HQ_RISK_PCT", 0.005):
            result = execute.maybe_execute_live(
                _hq_suggestion(entry=2000.0), 2000.0, cycle_id="c1", source="hq"
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "shadow")
        self.assertAlmostEqual(result["qty"], 0.1)
        self.assertAlmostEqual(result["notional_usd"], 200.0)
        self.assertEqual(result["instrument"], "ETP-20DEC30-CDE")


    def test_mill_clip_is_always_one_contract(self) -> None:
        # BTC mill clips are 0.01 regardless of spot; the sleeve check is
        # what rejects a contract whose notional no longer fits.
        result = execute.maybe_execute_live(
            _hq_suggestion(product_id="BTC-USD", entry=90000.0, stop_loss=88000.0),
            90000.0,
            cycle_id="c1",
            source="mill",
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["qty"], 0.01)
        self.assertAlmostEqual(result["notional_usd"], 900.0)

    def test_mill_clip_too_large_for_sleeve_is_skipped(self) -> None:
        # One BTC contract at $200k is $2,000 > the $1,400 sleeve at 1x.
        with patch.object(bot_config, "LIVE_MAX_LEVERAGE", 1.0):
            result = execute.maybe_execute_live(
                _hq_suggestion(product_id="BTC-USD", entry=200000.0, stop_loss=196000.0),
                200000.0,
                cycle_id="c1",
                source="mill",
            )
        self.assertIsNone(result)

    def test_mill_eth_clip_survives_a_high_eth_price(self) -> None:
        result = execute.maybe_execute_live(
            _hq_suggestion(entry=3000.0, stop_loss=2940.0),
            3000.0,
            cycle_id="c1",
            source="mill",
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["qty"], 0.1)
        self.assertAlmostEqual(result["notional_usd"], 300.0)

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

    def test_exposure_cap_refuses_when_not_even_one_contract_fits(self) -> None:
        # $1,950 already open against a $2,000 ceiling leaves $50 of headroom,
        # short of the $200 a single ETH nano costs — so there is nothing to
        # trim down to and the idea is refused rather than sized to zero.
        live_ledger.record_open(
            cycle_id="c1",
            source="hq",
            product_id="ETH-USD",
            instrument="ETH_USDC-PERPETUAL",
            side="long",
            qty=0.975,
            entry=2000.0,
            stop_loss=1940.0,
            take_profits_json="[]",
            order_id=None,
            stop_order_id=None,
            notes="ob:ob-0",
        )
        with patch.object(bot_config, "LIVE_MAX_LEVERAGE", 1.0):
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
        result = execute.maybe_execute_live(
            _hq_suggestion(entry=2000.0), 2000.0, cycle_id="c1", source="mill"
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["qty"], 0.1)
        self.assertAlmostEqual(result["notional_usd"], 200.0)

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


class MillSleeveTests(unittest.TestCase):
    """The two mill entry paths: auto FIFO self-fill and operator Accept."""

    OPERATOR = 8282981740

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmpdir.name) / "test_ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "EXECUTION_MODE", "shadow"),
            patch.object(bot_config, "CASE_STUDY_ENABLED", False),
            patch.object(execute, "_notify_ops"),
            patch.object(
                execute,
                "INSTRUMENT_MAP",
                {"ETH-USD": "ETP-20DEC30-CDE", "BTC-USD": "BIP-20DEC30-CDE"},
            ),
            # These cases are about the sleeve gates. Revalidation is off so the
            # synthetic levels are not judged against the live ETH mark (and so
            # the suite makes no network call); it has its own tests.
            patch.object(bot_config, "LIVE_REVALIDATE_ON_FILL", False),
        ]
        for p in self._patches:
            p.start()
        live_ledger.init_db()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _open_mill_clip(self, n: int = 1, **overrides) -> None:
        for i in range(n):
            kwargs = dict(
                cycle_id=f"mill_{i}",
                source="mill",
                product_id="ETH-USD",
                instrument="ETP-20DEC30-CDE",
                side="long",
                qty=0.1,
                entry=3000.0,
                stop_loss=2940.0,
                take_profits_json="[]",
                order_id=None,
                stop_order_id=None,
            )
            kwargs.update(overrides)
            live_ledger.record_open(**kwargs)

    def _idea(self, **overrides) -> dict:
        base = dict(
            idea_id=1,
            product_id="ETH-USD",
            direction="long",
            entry=3000.0,
            stop_loss=2940.0,
            take_profits=[3100.0],
            confidence=0.6,
        )
        base.update(overrides)
        return base

    # -- auto (FIFO) path -----------------------------------------------------

    def test_auto_fills_an_empty_sleeve(self) -> None:
        verdict = execute.execute_mill_idea(**self._idea())
        self.assertTrue(verdict["executed"])
        self.assertEqual(verdict["result"]["fill_type"], "auto")

    def test_auto_skips_below_the_conviction_floor(self) -> None:
        verdict = execute.execute_mill_idea(**self._idea(confidence=0.45))
        self.assertFalse(verdict["executed"])
        self.assertEqual(verdict["skip_reason"], "low_conviction")

    def test_auto_skips_a_missing_confidence(self) -> None:
        verdict = execute.execute_mill_idea(**self._idea(confidence=None))
        self.assertEqual(verdict["skip_reason"], "low_conviction")

    def test_auto_never_takes_a_second_slot(self) -> None:
        """Slots beyond the first are reserved for operator Accepts."""
        self._open_mill_clip(1)
        verdict = execute.execute_mill_idea(**self._idea())
        self.assertFalse(verdict["executed"])
        self.assertEqual(verdict["skip_reason"], "book_not_empty")

    def test_auto_can_be_disabled(self) -> None:
        with patch.object(bot_config, "LIVE_MILL_AUTO_FILL_ENABLED", False):
            verdict = execute.execute_mill_idea(**self._idea())
        self.assertEqual(verdict["skip_reason"], "auto_disabled")

    # -- loss cooldown ---------------------------------------------------------

    def _close_streak(
        self, n: int, *, product_id: str = "ETH-USD", side: str = "long",
        pnl: float = -2.0,
    ) -> None:
        """Book n consecutive closed mill trades on one (product, side)."""
        instrument = "ETP-20DEC30-CDE" if product_id == "ETH-USD" else "BIP-20DEC30-CDE"
        for i in range(n):
            tid = live_ledger.record_open(
                cycle_id=f"mill_cd_{product_id}_{side}_{i}",
                source="mill",
                product_id=product_id,
                instrument=instrument,
                side=side,
                qty=0.1,
                entry=3000.0,
                stop_loss=2940.0,
                take_profits_json="[]",
                order_id=None,
                stop_order_id=None,
            )
            live_ledger.record_close(
                tid, exit_price=2940.0, pnl_usd=pnl,
                close_reason="stop_loss" if pnl <= 0 else "take_profit",
            )

    def test_auto_sits_out_after_a_loss_streak(self) -> None:
        self._close_streak(bot_config.LIVE_MILL_LOSS_COOLDOWN_N)
        verdict = execute.execute_mill_idea(**self._idea())
        self.assertFalse(verdict["executed"])
        self.assertEqual(verdict["skip_reason"], "loss_cooldown")
        self.assertIn("until", verdict["cooldown"])

    def test_other_product_or_side_stays_eligible(self) -> None:
        """The sweep should move on to a different idea, not go quiet."""
        self._close_streak(bot_config.LIVE_MILL_LOSS_COOLDOWN_N)
        verdict = execute.execute_mill_idea(
            **self._idea(direction="short", entry=3000.0, stop_loss=3060.0,
                         take_profits=[2900.0])
        )
        self.assertTrue(verdict["executed"])

    def test_a_winner_breaks_the_streak(self) -> None:
        self._close_streak(bot_config.LIVE_MILL_LOSS_COOLDOWN_N - 1)
        self._close_streak(1, pnl=4.0)
        verdict = execute.execute_mill_idea(**self._idea())
        self.assertTrue(verdict["executed"])

    def test_cooldown_expires(self) -> None:
        self._close_streak(bot_config.LIVE_MILL_LOSS_COOLDOWN_N)
        with patch.object(bot_config, "LIVE_MILL_LOSS_COOLDOWN_MIN", 0):
            verdict = execute.execute_mill_idea(**self._idea())
        self.assertTrue(verdict["executed"])

    def test_manual_accept_ignores_the_cooldown(self) -> None:
        self._close_streak(bot_config.LIVE_MILL_LOSS_COOLDOWN_N)
        verdict = execute.execute_mill_idea(
            **self._idea(fill_type="manual", accepted_by=self.OPERATOR)
        )
        self.assertTrue(verdict["executed"])

    def test_cooldown_can_be_disabled(self) -> None:
        self._close_streak(bot_config.LIVE_MILL_LOSS_COOLDOWN_N)
        with patch.object(bot_config, "LIVE_MILL_LOSS_COOLDOWN_ENABLED", False):
            verdict = execute.execute_mill_idea(**self._idea())
        self.assertTrue(verdict["executed"])

    # -- manual (operator Accept) path ---------------------------------------

    def test_manual_fills_beside_an_existing_clip(self) -> None:
        self._open_mill_clip(1)
        verdict = execute.execute_mill_idea(
            **self._idea(fill_type="manual", accepted_by=self.OPERATOR)
        )
        self.assertTrue(verdict["executed"])
        self.assertEqual(verdict["result"]["fill_type"], "manual")
        self.assertEqual(verdict["result"]["filled_by"], self.OPERATOR)

    def test_manual_ignores_the_conviction_floor(self) -> None:
        verdict = execute.execute_mill_idea(
            **self._idea(
                confidence=0.1, fill_type="manual", accepted_by=self.OPERATOR
            )
        )
        self.assertTrue(verdict["executed"])

    def test_manual_rejects_an_unknown_telegram_id(self) -> None:
        verdict = execute.execute_mill_idea(
            **self._idea(fill_type="manual", accepted_by=12345)
        )
        self.assertFalse(verdict["executed"])
        self.assertEqual(verdict["skip_reason"], "not_authorized")

    def test_a_funded_testers_accept_fills(self) -> None:
        """The shipped half-measure. `LIVE_MILL_ANY_ACCEPT_FILLS` widened the
        Accept handler's gate but not this one, so a funded tester passed the
        check that decides whether to *try* and failed the check that decides
        whether to *fill* — reported as `not_authorized` after their budget
        had already been reserved."""
        tester = 555000111
        with patch.object(execute, "_may_fill", return_value=True):
            verdict = execute.execute_mill_idea(
                **self._idea(fill_type="manual", accepted_by=tester)
            )
        self.assertTrue(verdict["executed"], verdict.get("skip_reason"))

    def test_the_executor_defers_to_one_authorization_rule(self) -> None:
        """Holding the allowlist in two places is what caused the above."""
        import trade_ideas_bridge

        with patch.object(trade_ideas_bridge, "may_fill", return_value=True) as gate:
            execute.execute_mill_idea(
                **self._idea(fill_type="manual", accepted_by=777)
            )
        gate.assert_called_once_with(777)

    def test_a_dry_run_places_nothing(self) -> None:
        """`would_fill` has to be answerable without sending an order."""
        with patch.object(execute, "maybe_execute_live") as place:
            verdict = execute.execute_mill_idea(
                **self._idea(fill_type="manual", accepted_by=self.OPERATOR),
                dry_run=True,
            )
        place.assert_not_called()
        self.assertTrue(verdict["would_fill"])
        self.assertFalse(verdict["executed"])
        self.assertIsNone(verdict["result"])

    def test_a_dry_run_reports_the_same_refusal_as_a_real_attempt(self) -> None:
        self._open_mill_clip(bot_config.LIVE_MILL_MAX_OPEN)
        verdict = execute.execute_mill_idea(
            **self._idea(fill_type="manual", accepted_by=self.OPERATOR),
            dry_run=True,
        )
        self.assertFalse(verdict.get("would_fill"))
        self.assertEqual(verdict["skip_reason"], "sleeve_full")

    def test_manual_at_max_reports_sleeve_full(self) -> None:
        """The 'too many trades' notification depends on this exact reason."""
        self._open_mill_clip(bot_config.LIVE_MILL_MAX_OPEN)
        verdict = execute.execute_mill_idea(
            **self._idea(fill_type="manual", accepted_by=self.OPERATOR)
        )
        self.assertFalse(verdict["executed"])
        self.assertEqual(verdict["skip_reason"], "sleeve_full")
        self.assertEqual(verdict["capacity"]["open"], bot_config.LIVE_MILL_MAX_OPEN)
        self.assertEqual(verdict["capacity"]["slots_free"], 0)
        self.assertEqual(
            len(verdict["capacity"]["open_trades"]), bot_config.LIVE_MILL_MAX_OPEN
        )

    def test_halt_blocks_a_manual_accept(self) -> None:
        execute.halt_live("daily_loss:mill:-200.00")
        verdict = execute.execute_mill_idea(
            **self._idea(fill_type="manual", accepted_by=self.OPERATOR)
        )
        self.assertEqual(verdict["skip_reason"], "halted")

    # -- capacity + attribution ----------------------------------------------

    def test_three_clips_fit_the_funded_sleeve(self) -> None:
        self._open_mill_clip(bot_config.LIVE_MILL_MAX_OPEN)
        capacity = execute.mill_capacity()
        self.assertEqual(capacity["open"], 3)
        self.assertEqual(capacity["slots_free"], 0)
        self.assertLessEqual(
            capacity["open_notional_usd"], bot_config.LIVE_MILL_SLEEVE_USD
        )

    def test_fill_type_is_persisted_and_reported(self) -> None:
        self._open_mill_clip(1, fill_type="manual", filled_by=self.OPERATOR)
        self._open_mill_clip(1, cycle_id="mill_auto")
        rows = live_ledger.get_open_trades(source="mill")
        by_type = {r["fill_type"]: r for r in rows}
        self.assertEqual(set(by_type), {"auto", "manual"})
        self.assertEqual(by_type["manual"]["filled_by"], self.OPERATOR)
        self.assertIsNone(by_type["auto"]["filled_by"])
        perf = live_ledger.get_live_performance()
        self.assertEqual(perf["by_fill_type"]["mill"]["manual"]["open"], 1)
        self.assertEqual(perf["by_fill_type"]["mill"]["auto"]["open"], 1)


class LiveAlertRoutingTests(unittest.TestCase):
    """Both sleeves fill without a human in the loop, so an alert that reaches
    only one chat is a silent fill for everyone else."""

    def test_alerts_reach_admin_and_every_operator(self) -> None:
        with patch.object(config, "TELEGRAM_ADMIN_CHAT_ID", "999"), patch.object(
            bot_config, "LIVE_ALERT_TELEGRAM_IDS", (111, 222)
        ):
            self.assertEqual(execute._alert_chat_ids(), ["999", "111", "222"])

    def test_operator_doubling_as_admin_is_not_messaged_twice(self) -> None:
        with patch.object(config, "TELEGRAM_ADMIN_CHAT_ID", "111"), patch.object(
            bot_config, "LIVE_ALERT_TELEGRAM_IDS", (111, 222)
        ):
            self.assertEqual(execute._alert_chat_ids(), ["111", "222"])

    def test_missing_admin_chat_still_reaches_operators(self) -> None:
        with patch.object(config, "TELEGRAM_ADMIN_CHAT_ID", None), patch.object(
            config, "TELEGRAM_CHAT_ID", ""
        ), patch.object(bot_config, "LIVE_ALERT_TELEGRAM_IDS", (111,)):
            self.assertEqual(execute._alert_chat_ids(), ["111"])

    def test_one_unreachable_chat_does_not_silence_the_rest(self) -> None:
        sent: list[str] = []

        def _post(url, **kwargs):
            chat = kwargs.get("json", {}).get("chat_id")
            if chat == "111":
                raise RuntimeError("telegram down for this chat")
            sent.append(chat)

        mock_requests = MagicMock()
        mock_requests.post.side_effect = _post
        with patch.object(config, "TELEGRAM_ADMIN_CHAT_ID", "999"), patch.object(
            bot_config, "LIVE_ALERT_TELEGRAM_IDS", (111, 222)
        ), patch.object(config, "RESEND_API_KEY", ""), patch.dict(
            "sys.modules", {"requests": mock_requests}
        ):
            execute._notify_ops("fill")
        self.assertEqual(sent, ["999", "222"])


class HqClearsMillTests(unittest.TestCase):
    """Opposite mill must yield before HQ can share the contract."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmpdir.name) / "test_ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "EXECUTION_MODE", "live"),
            patch.object(bot_config, "CASE_STUDY_ENABLED", False),
            patch.object(bot_config, "LIVE_HQ_CLEARS_MILL", True),
            patch.object(bot_config, "LIVE_FILL_ALERTS_ENABLED", False),
            patch.object(execute, "_notify_ops"),
            patch.object(execute, "_SETTLE_SLEEP", 0),
            patch.object(
                execute,
                "INSTRUMENT_MAP",
                {"ETH-USD": "ETP-20DEC30-CDE", "BTC-USD": "BIP-20DEC30-CDE"},
            ),
        ]
        for p in self._patches:
            p.start()
        live_ledger.init_db()
        self.addCleanup(self._tmpdir.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)

    def _open_mill(self, **over) -> int:
        kwargs = dict(
            cycle_id="mill-1",
            source="mill",
            product_id="BTC-USD",
            instrument="BIP-20DEC30-CDE",
            side="long",
            qty=0.01,
            entry=81180.0,
            stop_loss=80437.0,
            take_profits_json="[82000]",
            order_id="mill-entry",
            stop_order_id=None,
            exit_order_ids=["mill-bracket"],
        )
        kwargs.update(over)
        return live_ledger.record_open(**kwargs)

    def _gateway(self, *, mark: float = 81030.0) -> MagicMock:
        gw = MagicMock()
        gw.contract_size.return_value = 0.01
        gw.get_position.return_value = {"size": 0.0, "mark_price": mark}
        cancelled: set[str] = set()

        def get_order(oid: str) -> dict:
            if oid in cancelled:
                return {"status": "CANCELLED"}
            return {
                "status": "OPEN",
                "order_configuration": {
                    "trigger_bracket_gtc": {
                        "base_size": "1",
                        "limit_price": "82000",
                        "stop_trigger_price": "80437",
                    }
                },
            }

        gw.get_order.side_effect = get_order
        gw.cancel_orders.side_effect = cancelled.update
        gw.place_market_order.side_effect = lambda **kw: {
            "order": {
                "order_id": f"mkt-{kw['side']}-{kw['amount']}",
                "average_price": mark,
                "filled_qty": kw["amount"],
            }
        }
        gw.place_bracket.side_effect = lambda **kw: {
            "order": {"order_id": f"br-{kw['limit_price']}"}
        }
        gw.place_stop_market.return_value = {"order": {"order_id": "stop-hq"}}
        return gw

    def test_opposing_mill_is_flattened_before_hq_entry(self) -> None:
        """The 2026-09-03 failure: HQ BTC short into a bracketed mill long."""
        mill_id = self._open_mill()
        gw = self._gateway()
        suggestion = _hq_suggestion(
            action="deriv_sell",
            product_id="BTC-USD",
            entry=81010.97,
            stop_loss=81700.0,
            take_profits=[79257.14, 78562.74],
            order_block_ref="btc-ob",
        )
        with patch.object(execute, "get_gateway", return_value=gw), patch.object(
            bot_config, "LIVE_HQ_RISK_PCT", 0.007
        ):
            result = execute.maybe_execute_live(
                suggestion, 81030.0, cycle_id="hq-btc-short", source="hq"
            )

        self.assertIsNotNone(result)
        mill = live_ledger.get_trade(mill_id)
        self.assertEqual(mill["status"], "closed")
        self.assertEqual(mill["close_reason"], "hq_priority")
        labels = [c.kwargs["label"] for c in gw.place_market_order.call_args_list]
        self.assertTrue(any(lab.startswith("mill-yield:") for lab in labels))
        self.assertTrue(any(lab.startswith("hq:") for lab in labels))
        gw.cancel_orders.assert_called()

    def test_same_direction_mill_is_left_alone(self) -> None:
        mill_id = self._open_mill(side="short", stop_loss=82000.0)
        gw = self._gateway()
        suggestion = _hq_suggestion(
            action="deriv_sell",
            product_id="BTC-USD",
            entry=81010.97,
            stop_loss=81700.0,
            take_profits=[79257.14],
            order_block_ref="btc-ob-2",
        )
        with patch.object(execute, "get_gateway", return_value=gw):
            result = execute.maybe_execute_live(
                suggestion, 81030.0, cycle_id="hq-btc-short-2", source="hq"
            )

        self.assertIsNotNone(result)
        self.assertEqual(live_ledger.get_trade(mill_id)["status"], "open")
        labels = [c.kwargs["label"] for c in gw.place_market_order.call_args_list]
        self.assertFalse(any(lab.startswith("mill-yield:") for lab in labels))
        gw.cancel_orders.assert_not_called()

    def test_flag_off_leaves_opposing_mill_in_place(self) -> None:
        mill_id = self._open_mill()
        gw = self._gateway()
        suggestion = _hq_suggestion(
            action="deriv_sell",
            product_id="BTC-USD",
            entry=81010.97,
            stop_loss=81700.0,
            take_profits=[79257.14],
            order_block_ref="btc-ob-3",
        )
        with patch.object(bot_config, "LIVE_HQ_CLEARS_MILL", False), patch.object(
            execute, "get_gateway", return_value=gw
        ):
            result = execute.maybe_execute_live(
                suggestion, 81030.0, cycle_id="hq-btc-short-3", source="hq"
            )

        self.assertIsNotNone(result)
        self.assertEqual(live_ledger.get_trade(mill_id)["status"], "open")
        labels = [c.kwargs["label"] for c in gw.place_market_order.call_args_list]
        self.assertFalse(any(lab.startswith("mill-yield:") for lab in labels))

    def test_mill_refill_is_skipped_after_hq_priority_close(self) -> None:
        mill_id = self._open_mill()
        gw = self._gateway()
        suggestion = _hq_suggestion(
            action="deriv_sell",
            product_id="BTC-USD",
            entry=81010.97,
            stop_loss=81700.0,
            take_profits=[79257.14],
            order_block_ref="btc-ob-4",
        )
        with patch.object(execute, "get_gateway", return_value=gw), patch.object(
            execute, "_refill_mill_sleeve"
        ) as refill:
            execute.maybe_execute_live(
                suggestion, 81030.0, cycle_id="hq-btc-short-4", source="hq"
            )
        refill.assert_not_called()
        self.assertEqual(live_ledger.get_trade(mill_id)["close_reason"], "hq_priority")

    def _open_family(self, source: str, **over) -> int:
        """An open mirror/control position holding the shared BTC contract."""
        kwargs = dict(
            cycle_id=f"{source}-1",
            source=source,
            product_id="BTC-USD",
            instrument="BIP-20DEC30-CDE",
            side="long",
            qty=0.01,
            entry=86095.0,
            stop_loss=83200.0,
            take_profits_json="[87000]",
            order_id=f"{source}-entry",
            stop_order_id=None,
            exit_order_ids=[f"{source}-bracket"],
        )
        kwargs.update(over)
        return live_ledger.record_open(**kwargs)

    def _btc_short(self, ref: str) -> Suggestion:
        return _hq_suggestion(
            action="deriv_sell",
            product_id="BTC-USD",
            entry=85906.24,
            stop_loss=86600.0,
            take_profits=[85000.0],
            order_block_ref=ref,
        )

    def test_swing_mirror_outranks_control(self) -> None:
        """Operator order: the swing mirror takes the contract off control."""
        hq_id = self._open_family("hq")
        gw = self._gateway(mark=85906.24)
        with patch.object(execute, "get_gateway", return_value=gw):
            result = execute.maybe_execute_live(
                self._btc_short("btc-ob-swing"),
                85906.24,
                cycle_id="var_eva_swing_llm_40",
                source="hq_swing",
            )

        self.assertIsNotNone(result)
        hq = live_ledger.get_trade(hq_id)
        self.assertEqual(hq["status"], "closed")
        self.assertEqual(hq["close_reason"], "hq_priority")
        labels = [c.kwargs["label"] for c in gw.place_market_order.call_args_list]
        self.assertTrue(any(lab.startswith("hq-yield:") for lab in labels))

    def test_swing_mirror_outranks_the_day_mirror(self) -> None:
        day_id = self._open_family("hq_day")
        gw = self._gateway(mark=85906.24)
        with patch.object(execute, "get_gateway", return_value=gw):
            result = execute.maybe_execute_live(
                self._btc_short("btc-ob-swing-2"),
                85906.24,
                cycle_id="var_eva_swing_llm_41",
                source="hq_swing",
            )

        self.assertIsNotNone(result)
        self.assertEqual(live_ledger.get_trade(day_id)["status"], "closed")

    def test_day_mirror_outranks_control(self) -> None:
        hq_id = self._open_family("hq")
        gw = self._gateway(mark=85906.24)
        with patch.object(execute, "get_gateway", return_value=gw):
            result = execute.maybe_execute_live(
                self._btc_short("btc-ob-day"),
                85906.24,
                cycle_id="var_eva_day_36",
                source="hq_day",
            )

        self.assertIsNotNone(result)
        self.assertEqual(live_ledger.get_trade(hq_id)["status"], "closed")

    def test_day_mirror_yields_to_swing(self) -> None:
        """Swing sits above day, so day refuses rather than flattening it."""
        swing_id = self._open_family("hq_swing")
        gw = self._gateway(mark=85906.24)
        with patch.object(execute, "get_gateway", return_value=gw):
            result = execute.maybe_execute_live(
                self._btc_short("btc-ob-tie"),
                85906.24,
                cycle_id="var_eva_day_37",
                source="hq_day",
            )

        self.assertIsNone(result)
        self.assertEqual(live_ledger.get_trade(swing_id)["status"], "open")
        gw.place_market_order.assert_not_called()

    def test_control_yields_to_a_mirror_holding_the_contract(self) -> None:
        """The 2026-09-21 case: control's BTC short into the swing mirror.

        Still refused under the operator order — but as a logged skip rather
        than a venue reject, so it no longer feeds a retry loop.
        """
        swing_id = self._open_family("hq_swing")
        gw = self._gateway(mark=85906.24)
        with patch.object(execute, "get_gateway", return_value=gw), patch.object(
            bot_config, "LIVE_HQ_RISK_PCT", 0.007
        ):
            result = execute.maybe_execute_live(
                self._btc_short("btc-ob-control"),
                85906.24,
                cycle_id="hq-btc-short-5",
                source="hq",
            )

        self.assertIsNone(result)
        self.assertEqual(live_ledger.get_trade(swing_id)["status"], "open")
        gw.place_market_order.assert_not_called()

    def test_mill_refuses_rather_than_letting_the_venue_reject(self) -> None:
        """Mill outranks nothing, so it skips instead of retrying into a reject."""
        swing_id = self._open_family("hq_swing")
        gw = self._gateway(mark=85906.24)
        with patch.object(execute, "get_gateway", return_value=gw):
            result = execute.maybe_execute_live(
                self._btc_short("btc-ob-mill"),
                85906.24,
                cycle_id="mill_1049",
                source="mill",
            )

        self.assertIsNone(result)
        self.assertEqual(live_ledger.get_trade(swing_id)["status"], "open")
        gw.place_market_order.assert_not_called()

    def test_same_direction_mirror_shares_the_contract(self) -> None:
        swing_id = self._open_family("hq_swing", side="short", stop_loss=87000.0)
        gw = self._gateway(mark=85906.24)
        with patch.object(execute, "get_gateway", return_value=gw), patch.object(
            bot_config, "LIVE_HQ_RISK_PCT", 0.007
        ):
            result = execute.maybe_execute_live(
                self._btc_short("btc-ob-same"),
                85906.24,
                cycle_id="hq-btc-short-6",
                source="hq",
            )

        self.assertIsNotNone(result)
        self.assertEqual(live_ledger.get_trade(swing_id)["status"], "open")


class PooledFillTests(unittest.TestCase):
    """Tester intents ride the house order: one aggregate fill, virtual shares.

    Entry 80,000, stop 79,300 → $700 risk per BTC unit, $7 per nano contract.
    Alice's $1,500 gives a $10.50 budget (0.7%) — one whole extra contract.
    """

    ADMIN = 111
    ALICE = 1001

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmpdir.name) / "test_ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "EXECUTION_MODE", "live"),
            patch.object(bot_config, "CASE_STUDY_ENABLED", False),
            patch.object(bot_config, "LIVE_FILL_ALERTS_ENABLED", False),
            patch.object(bot_config, "LIVE_HQ_CLEARS_MILL", False),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_RISK_PCT", 0.007),
            patch.object(bot_config, "POOL_MIN_EQUITY_USD", 500.0),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (self.ADMIN,)),
            patch.object(execute, "_notify_ops"),
            patch.object(execute, "_pool_dm"),
            patch.object(execute, "_SETTLE_SLEEP", 0),
            patch.object(
                execute,
                "INSTRUMENT_MAP",
                {"ETH-USD": "ETP-20DEC30-CDE", "BTC-USD": "BIP-20DEC30-CDE"},
            ),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmpdir.cleanup)
        live_ledger.init_db()

        import pool

        self.pool = pool
        pool.init_db()
        pool.approve_user(self.ALICE, admin_id=self.ADMIN)
        pool.credit(self.ALICE, 1500.0, admin_id=self.ADMIN)

    def _gateway(self, *, mark: float = 80_000.0) -> MagicMock:
        gw = MagicMock()
        gw.contract_size.return_value = 0.01
        gw.get_position.return_value = {"size": 0.0, "mark_price": mark}
        gw.get_order.return_value = {"status": "OPEN", "order_configuration": {}}
        gw.place_market_order.side_effect = lambda **kw: {
            "order": {
                "order_id": f"mkt-{kw['side']}-{kw['amount']}",
                "average_price": mark,
                "filled_qty": kw["amount"],
            }
        }
        gw.place_bracket.side_effect = lambda **kw: {
            "order": {"order_id": f"br-{kw['limit_price']}"}
        }
        gw.place_stop_market.return_value = {"order": {"order_id": "stop-x"}}
        return gw

    def _suggestion(self) -> Suggestion:
        return _hq_suggestion(
            product_id="BTC-USD",
            entry=80_000.0,
            stop_loss=79_300.0,
            take_profits=[80_700.0, 81_400.0],
            order_block_ref="pool-ob",
        )

    def test_mill_fill_adds_tester_contracts_and_opens_stakes(self) -> None:
        intent = self.pool.record_intent("mill_77", self.ALICE)
        self.assertTrue(intent["ok"])
        self.assertAlmostEqual(intent["risk_usd"], 10.5, places=2)

        gw = self._gateway()
        with patch.object(execute, "get_gateway", return_value=gw):
            result = execute.maybe_execute_live(
                self._suggestion(), 80_000.0, cycle_id="mill_77", source="mill"
            )

        self.assertIsNotNone(result)
        # House clip 0.01 + Alice's $10.50 budget = one extra $7 contract.
        self.assertAlmostEqual(result["qty"], 0.02, places=9)
        entry_call = gw.place_market_order.call_args_list[0]
        self.assertAlmostEqual(entry_call.kwargs["amount"], 0.02, places=9)

        stakes = self.pool.open_stakes_for(int(result["trade_id"]))
        self.assertEqual(len(stakes), 1)
        stake = stakes[0]
        # Split by budgets at the fill: house $7 vs Alice $10.50 of $17.50.
        self.assertAlmostEqual(stake["share_frac"], 10.5 / 17.5, places=6)
        self.assertAlmostEqual(stake["qty"], 0.02 * 10.5 / 17.5, places=9)
        # Her intent was consumed — nothing left pending on the ref.
        self.assertEqual(self.pool.pending_intents("mill_77"), [])
        # Reserve now holds the stake margin, not the intent budget.
        account = self.pool.get_account(self.ALICE)
        self.assertAlmostEqual(
            float(account["reserved_usd"]), stake["cost_usd"], places=2
        )
        execute._pool_dm.assert_called()

    def test_hq_fill_pools_the_same_way(self) -> None:
        intent = self.pool.record_intent("hq-cycle-9", self.ALICE)
        self.assertTrue(intent["ok"])

        gw = self._gateway()
        with patch.object(execute, "get_gateway", return_value=gw):
            result = execute.maybe_execute_live(
                self._suggestion(), 80_000.0, cycle_id="hq-cycle-9", source="hq"
            )

        self.assertIsNotNone(result)
        stakes = self.pool.open_stakes_for(int(result["trade_id"]))
        self.assertEqual(len(stakes), 1)
        # The house vault clip still exists under her share: the fill is
        # strictly larger than the house-only qty.
        house_qty = float(result["qty"]) - 0.01  # her budget bought 1 contract
        self.assertGreater(house_qty, 0)

    def test_pool_off_leaves_the_order_house_sized(self) -> None:
        self.pool.record_intent("mill_88", self.ALICE)
        gw = self._gateway()
        with patch.object(bot_config, "POOL_ENABLED", False), patch.object(
            execute, "get_gateway", return_value=gw
        ):
            result = execute.maybe_execute_live(
                self._suggestion(), 80_000.0, cycle_id="mill_88", source="mill"
            )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["qty"], 0.01, places=9)
        self.assertEqual(self.pool.open_stakes_for(int(result["trade_id"])), [])

    def test_exit_leg_credits_her_share_through_the_reconcile_hook(self) -> None:
        self.pool.record_intent("mill_99", self.ALICE)
        gw = self._gateway()
        with patch.object(execute, "get_gateway", return_value=gw):
            result = execute.maybe_execute_live(
                self._suggestion(), 80_000.0, cycle_id="mill_99", source="mill"
            )
        trade_id = int(result["trade_id"])
        stake = self.pool.open_stakes_for(trade_id)[0]

        # TP1 leg fills on the venue: 0.01 @ 80,700 → +$7 on the whole clip.
        exit_oid = result and "tp1-fill"
        gw.get_order.side_effect = lambda oid: (
            {
                "status": "FILLED",
                "filled_size": 1,          # 1 contract × 0.01 size
                "average_filled_price": 80_700.0,
                "order_configuration": {
                    "trigger_bracket_gtc": {
                        "limit_price": "80700",
                        "stop_trigger_price": "79300",
                    }
                },
            }
            if oid == exit_oid
            else {"status": "OPEN", "order_configuration": {}}
        )
        trade = live_ledger.get_trade(trade_id)
        ids = json.loads(trade.get("exit_order_ids_json") or "[]")
        ids[0] = exit_oid
        import sqlite3 as _sq

        conn = _sq.connect(config.LEDGER_DB)
        conn.execute(
            "UPDATE live_trades SET exit_order_ids_json = ? WHERE id = ?",
            (json.dumps(ids), trade_id),
        )
        conn.commit()
        conn.close()

        with patch.object(execute, "get_gateway", return_value=gw):
            execute.sync_live_positions()

        account = self.pool.get_account(self.ALICE)
        expected = round(7.0 * stake["share_frac"], 2)
        self.assertAlmostEqual(
            float(account["cash_usd"]), 1500.0 + expected, places=2
        )


if __name__ == "__main__":
    unittest.main()
