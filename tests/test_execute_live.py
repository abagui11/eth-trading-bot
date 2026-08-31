"""Tests for live execution sizing and kill switches (execute.py).

All tests run in EXECUTION_MODE=off/shadow — no gateway is ever contacted.
"""

from __future__ import annotations

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

    def test_mill_clip_rounds_up_to_one_contract(self) -> None:
        # A $260 target on BTC at $90k is 0.0029 BTC, under the 0.01 contract
        # floor. Rounding up to one contract ($900) is what makes BTC ideas
        # fillable at all; the sleeve check below is the real guard.
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
        result = execute.maybe_execute_live(
            _hq_suggestion(product_id="BTC-USD", entry=200000.0, stop_loss=196000.0),
            200000.0,
            cycle_id="c1",
            source="mill",
        )
        self.assertIsNone(result)

    def test_mill_eth_clip_survives_a_high_eth_price(self) -> None:
        # The regression this rounding fixes: at ETH $3,000 a flat $260 clip is
        # 0.087 ETH, under the 0.1 floor, and used to abort the fill entirely.
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


class MillSleeveTests(unittest.TestCase):
    """The two mill entry paths: auto FIFO self-fill and operator Accept."""

    OPERATOR = 8282981740

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmpdir.name) / "test_ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "EXECUTION_MODE", "shadow"),
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


if __name__ == "__main__":
    unittest.main()
