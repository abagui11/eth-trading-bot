"""A resting order has to report back.

The card says "it executes only if BTC rises to $78,783.23". Before this, every
way that promise could end -- filling, being pulled on a fresh read, expiring,
or price gapping through both entry and stop -- was silent, so the card stayed
on the subscriber's screen looking live forever. These tests pin that each
ending reaches exactly the people who were told the promise, and nobody else.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import execute
import live_pending
import notify
from models import Suggestion


def _sug(action: str = "deriv_sell", entry: float = 2500.0, stop: float = 2540.0):
    return Suggestion(
        action=action,
        size=100.0,
        entry=entry,
        stop_loss=stop,
        take_profits=[2440.0, 2400.0],
        risk_reward=1.5,
        rationale="M5 order-block fib entry.",
        order_block={},
        product_id="ETH-USD",
    )


class PendingNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db = Path(self._tmp.name) / "ledger.db"
        for p in (
            patch.object(config, "LEDGER_DB", self._db),
            patch.object(config, "EXECUTION_MODE", "live"),
            patch.object(bot_config, "LIVE_PENDING_ENTRIES_ENABLED", True),
            patch.object(bot_config, "LIVE_PENDING_EXPIRY_HOURS", 4.0),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        live_pending.init_db()

    def _park(self, *, recipients=(111, 222), **kw) -> None:
        live_pending.record(_sug(**kw), cycle_id="c1")
        if recipients:
            live_pending.set_recipients("ETH-USD", list(recipients))

    # --- who gets told -------------------------------------------------

    def test_recipients_survive_a_round_trip(self) -> None:
        self._park(recipients=(222, 111, 111))

        row = live_pending.get_pending("ETH-USD")[0]
        self.assertEqual(live_pending.recipients_of(row), [111, 222])

    def test_a_fresh_plan_does_not_inherit_the_old_audience(self) -> None:
        """record() replaces the row, so the new plan starts with nobody."""
        self._park()
        live_pending.record(_sug(entry=2510.0), cycle_id="c2")

        row = live_pending.get_pending("ETH-USD")[0]
        self.assertEqual(live_pending.recipients_of(row), [])

    def test_a_plan_nobody_was_told_about_sends_nothing(self) -> None:
        self._park(recipients=())

        with patch.object(notify, "send_pending_notice") as send:
            live_pending.cancel("ETH-USD")

        send.assert_not_called()

    def test_a_telegram_failure_cannot_break_the_sweep(self) -> None:
        """The sweep reconciles live positions after this."""
        self._park()

        with patch.object(notify, "send_pending_notice", side_effect=RuntimeError):
            with self.assertLogs("live_pending", level="ERROR"):
                dropped = live_pending.cancel("ETH-USD")

        self.assertEqual(dropped, 1)
        self.assertEqual(live_pending.get_pending(), [])

    # --- each ending ---------------------------------------------------

    def test_a_fill_is_announced_with_the_price_it_filled_at(self) -> None:
        self._park()

        with patch.object(execute, "maybe_execute_live") as fire:
            fire.return_value = {"mode": "live", "fill": 2501.5}
            with patch.object(notify, "send_pending_notice") as send:
                live_pending.sweep({"ETH-USD": 2505.0})

        self.assertEqual(send.call_args.kwargs["outcome"], "filled")
        self.assertEqual(send.call_args.kwargs["fill"], 2501.5)

    def test_a_shadow_order_is_never_announced_as_a_fill(self) -> None:
        """Shadow logs a payload and sends nothing. Announcing it would
        describe a position that does not exist."""
        self._park()

        with patch.object(execute, "maybe_execute_live") as fire:
            fire.return_value = {"mode": "shadow"}
            with patch.object(notify, "send_pending_notice") as send:
                live_pending.sweep({"ETH-USD": 2505.0})

        send.assert_not_called()

    def test_a_refused_order_says_no_position_was_taken(self) -> None:
        self._park()

        with patch.object(execute, "maybe_execute_live", return_value=None):
            with patch.object(notify, "send_pending_notice") as send:
                live_pending.sweep({"ETH-USD": 2505.0})

        self.assertEqual(send.call_args.kwargs["outcome"], "refused")

    def test_a_gap_through_entry_and_stop_reports_the_setup_gone(self) -> None:
        self._park()

        with patch.object(execute, "maybe_execute_live") as fire:
            with patch.object(notify, "send_pending_notice") as send:
                live_pending.sweep({"ETH-USD": 2560.0})

        fire.assert_not_called()
        self.assertEqual(send.call_args.kwargs["outcome"], "missed")

    def test_an_expiry_is_announced(self) -> None:
        self._park()
        conn = sqlite3.connect(self._db)
        conn.execute(
            "UPDATE live_pending SET created_at = ? WHERE product_id = ?",
            ("2020-01-01T00:00:00Z", "ETH-USD"),
        )
        conn.commit()
        conn.close()

        with patch.object(notify, "send_pending_notice") as send:
            live_pending.sweep({"ETH-USD": 2450.0})

        self.assertEqual(send.call_args.kwargs["outcome"], "expired")

    def test_a_decline_is_announced_but_a_market_takeover_is_not(self) -> None:
        """A no_trade leaves silence behind, so it needs a notice. Being taken
        at market sends a fresh card in the same breath, so it does not."""
        self._park()
        with patch.object(notify, "send_pending_notice") as send:
            live_pending.cancel("ETH-USD")
        self.assertEqual(send.call_args.kwargs["outcome"], "cancelled")

        self._park()
        with patch.object(notify, "send_pending_notice") as send:
            live_pending.cancel("ETH-USD", reason="taken at market", outcome=None)
        send.assert_not_called()

    def test_a_plan_still_waiting_is_not_announced(self) -> None:
        self._park()

        with patch.object(notify, "send_pending_notice") as send:
            live_pending.sweep({"ETH-USD": 2450.0})

        send.assert_not_called()
        self.assertEqual(len(live_pending.get_pending()), 1)


class NoticeWordingTests(unittest.TestCase):
    ROW = {
        "product_id": "BTC-USD",
        "action": "deriv_sell",
        "side": "short",
        "entry": 78783.23,
        "stop_loss": 79452.0,
        "size": 1307.45,
        "risk_reward": 1.41,
        "take_profits_json": "[77840.58]",
        "notify_ids": json.dumps([111]),
    }

    def test_a_fill_names_the_fill_price_and_not_the_mark(self) -> None:
        text = notify.format_pending_notice(
            self.ROW, outcome="filled", spot=78999.0, fill=78790.0
        )

        self.assertIn("$78,790.00", text)
        # Two different numbers for the same event reads as a discrepancy.
        self.assertNotIn("78,999", text)
        self.assertIn("short is open", text)

    def test_every_unfilled_ending_says_no_position_was_taken(self) -> None:
        for outcome in ("refused", "missed", "cancelled", "expired"):
            with self.subTest(outcome=outcome):
                text = notify.format_pending_notice(
                    self.ROW, outcome=outcome, spot=77743.06, hours=4.0
                )
                self.assertIn("No position was taken", text)
                self.assertIn("Never filled", text)
                # The entry the card promised has to appear, so the
                # subscriber can tie the notice to the card.
                self.assertIn("$78,783.23", text)

    def test_the_headline_is_the_first_line_so_it_can_be_bolded(self) -> None:
        text = notify.format_pending_notice(self.ROW, outcome="filled")
        headline = text.partition("\n")[0]

        self.assertIn("BTC Deriv Sell", headline)
        self.assertIn("Filled", headline)


class PriceDirectionTests(unittest.TestCase):
    """The percentages are position returns, so their sign does not say which
    way price moves. On a short it points the opposite way."""

    def test_a_short_falls_into_its_target_and_rises_into_its_stop(self) -> None:
        import display_summary

        self.assertEqual(
            display_summary.price_direction("deriv_sell", toward_target=True), "falls"
        )
        self.assertEqual(
            display_summary.price_direction("spot_sell", toward_target=False), "rises"
        )

    def test_a_long_rises_into_its_target_and_falls_into_its_stop(self) -> None:
        import display_summary

        self.assertEqual(
            display_summary.price_direction("deriv_buy", toward_target=True), "rises"
        )
        self.assertEqual(
            display_summary.price_direction("spot_buy", toward_target=False), "falls"
        )

    def test_the_short_card_no_longer_calls_a_gain_a_price_move(self) -> None:
        import display_summary

        body = display_summary.build_card_body(
            _sug(action="deriv_sell", entry=2500.0, stop=2540.0),
            display_summary="Bearish structure.",
        )

        self.assertIn("+2.40% if price falls to it", body)
        self.assertIn("-1.60% if price rises to it", body)
        self.assertNotIn("price move", body)


if __name__ == "__main__":
    unittest.main()
