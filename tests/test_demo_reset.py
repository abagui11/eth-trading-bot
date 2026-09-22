"""The demo-account reset: back to approved-but-empty between recording takes.

A run-through leaves an open stake in a real mill trade, a subscription with
an allocation, and leftover cash. `demo_card.reset_account` must unwind all
of it — with the stake settled honestly (P&L share credited, margin released)
rather than only deleted — and `/resetdemo` must preview before it acts,
because nothing but the typed id separates the demo account from a tester.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bot
import bot_config
import config
import demo_card
import execute
import live_ledger
import pool
from models import Suggestion

ADMIN = 111
DEMO = 8708390551


def _mill_suggestion() -> Suggestion:
    return Suggestion(
        action="deriv_buy",
        size=0.5,
        entry=80_000.0,
        stop_loss=79_300.0,
        take_profits=[80_700.0, 81_400.0],
        risk_reward=2.0,
        rationale="test",
        product_id="BTC-USD",
        order_block_ref="reset-ob",
    )


class _Harness(unittest.TestCase):
    """Real pool + live ledger on a temp DB; MagicMock gateway; no network."""

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
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
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
        pool.init_db()
        pool.approve_user(DEMO, admin_id=ADMIN)
        pool.credit(DEMO, 500.0, admin_id=ADMIN)
        pool.subscribe_strategy(DEMO, "mill")
        pool.set_allocation(DEMO, "mill", 250.0)

    def _gateway(self, *, mark: float = 80_000.0) -> MagicMock:
        gw = MagicMock()
        gw.contract_size.return_value = 0.01
        gw.get_position.return_value = {"size": 0.0, "mark_price": mark}
        gw.get_order.return_value = {"status": "CANCELLED"}
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

    def _open_staked_trade(self, gw: MagicMock) -> int:
        """A real mill fill with the demo account's intent riding it."""
        self.assertTrue(pool.record_intent("mill_9", DEMO)["ok"])
        with patch.object(execute, "get_gateway", return_value=gw):
            result = execute.maybe_execute_live(
                _mill_suggestion(), 80_000.0, cycle_id="mill_9", source="mill"
            )
        self.assertIsNotNone(result)
        return int(result["trade_id"])


class CloseLiveTradeTests(_Harness):
    def test_close_books_the_stake_share_and_frees_the_margin(self) -> None:
        gw = self._gateway()
        tid = self._open_staked_trade(gw)
        before = pool.get_account(DEMO)
        self.assertGreater(float(before["reserved_usd"]), 0)

        with patch.object(execute, "get_gateway", return_value=self._gateway(mark=80_700.0)):
            result = execute.close_live_trade(tid)

        self.assertTrue(result["ok"], result)
        self.assertEqual(live_ledger.get_trade(tid)["status"], "closed")
        stakes = pool.open_stakes_for(tid)
        self.assertEqual(stakes, [])
        after = pool.get_account(DEMO)
        self.assertEqual(float(after["reserved_usd"]), 0.0)
        # The tester's P&L share was credited, not just the margin returned:
        # +700/BTC on their share of the fill means cash strictly above the
        # pre-trade balance.
        self.assertGreater(float(after["cash_usd"]), float(before["cash_usd"]))

    def test_close_flattens_only_its_own_size(self) -> None:
        gw = self._gateway()
        tid = self._open_staked_trade(gw)
        trade = live_ledger.get_trade(tid)
        close_gw = self._gateway()
        with patch.object(execute, "get_gateway", return_value=close_gw):
            execute.close_live_trade(tid)
        flatten = close_gw.place_market_order.call_args
        self.assertEqual(flatten.kwargs["side"], "sell")
        self.assertAlmostEqual(
            flatten.kwargs["amount"], float(trade["qty_open"]), places=9
        )

    def test_close_refuses_a_trade_that_is_not_open(self) -> None:
        gw = self._gateway()
        tid = self._open_staked_trade(gw)
        with patch.object(execute, "get_gateway", return_value=self._gateway()):
            execute.close_live_trade(tid)
            again = execute.close_live_trade(tid)
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "not_open")

    def test_close_does_not_refill_the_mill_sleeve(self) -> None:
        gw = self._gateway()
        tid = self._open_staked_trade(gw)
        with patch.object(execute, "get_gateway", return_value=self._gateway()), \
                patch.object(execute, "_refill_mill_sleeve") as refill:
            execute.close_live_trade(tid)
        refill.assert_not_called()


class ResetAccountTests(_Harness):
    def test_reset_returns_the_account_to_empty(self) -> None:
        gw = self._gateway()
        self._open_staked_trade(gw)

        with patch.object(execute, "get_gateway", return_value=self._gateway()):
            result = demo_card.reset_account(DEMO, admin_id=ADMIN)

        self.assertTrue(result["ok"], result)
        account = pool.get_account(DEMO)
        self.assertAlmostEqual(float(account["cash_usd"]), 0.0, places=2)
        self.assertEqual(float(account["reserved_usd"]), 0.0)
        self.assertEqual(pool.strategy_subscriptions(DEMO), [])
        self.assertEqual(
            [v for v in pool.allocations(DEMO).values() if v > 0], []
        )
        self.assertEqual(pool.open_stake_trade_ids(DEMO), [])
        # Still approved: the next take starts with /credit, not re-admission.
        self.assertTrue(pool.is_approved(DEMO))

    def test_dry_run_reports_and_touches_nothing(self) -> None:
        gw = self._gateway()
        tid = self._open_staked_trade(gw)

        report = demo_card.reset_account(DEMO, admin_id=ADMIN, dry_run=True)

        self.assertTrue(report["ok"])
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["trade_ids"], [tid])
        self.assertEqual(report["subscriptions"], ["mill"])
        self.assertEqual(live_ledger.get_trade(tid)["status"], "open")
        self.assertEqual(pool.strategy_subscriptions(DEMO), ["mill"])
        self.assertGreater(float(pool.get_account(DEMO)["cash_usd"]), 0)

    def test_a_failed_close_stops_before_the_debit(self) -> None:
        gw = self._gateway()
        self._open_staked_trade(gw)
        cash_before = float(pool.get_account(DEMO)["cash_usd"])

        with patch.object(
            execute, "close_live_trade",
            return_value={"ok": False, "reason": "flatten_rejected"},
        ):
            result = demo_card.reset_account(DEMO, admin_id=ADMIN)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "close_failed")
        # Nothing else moved: still subscribed, cash untouched.
        self.assertEqual(pool.strategy_subscriptions(DEMO), ["mill"])
        self.assertEqual(float(pool.get_account(DEMO)["cash_usd"]), cash_before)

    def test_an_unknown_account_is_refused(self) -> None:
        result = demo_card.reset_account(424242, admin_id=ADMIN)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_account")


class ResetDemoCommandTests(_Harness):
    def _run(self, caller: int, args: list[str]) -> str:
        update = MagicMock()
        update.effective_user.id = caller
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = args
        asyncio.run(bot.cmd_resetdemo(update, context))
        return "\n".join(
            str(c.args[0]) for c in update.message.reply_text.call_args_list
        )

    def test_only_admins_can_reset(self) -> None:
        reply = self._run(DEMO, [str(DEMO), "confirm"])
        self.assertEqual(reply, "")
        self.assertEqual(pool.strategy_subscriptions(DEMO), ["mill"])

    def test_without_confirm_it_only_previews(self) -> None:
        gw = self._gateway()
        tid = self._open_staked_trade(gw)
        reply = self._run(ADMIN, [str(DEMO)])
        self.assertIn("nothing done yet", reply)
        self.assertIn(f"#{tid}", reply)
        self.assertIn("confirm", reply)
        self.assertEqual(live_ledger.get_trade(tid)["status"], "open")
        self.assertEqual(pool.strategy_subscriptions(DEMO), ["mill"])

    def test_confirm_runs_the_reset(self) -> None:
        gw = self._gateway()
        tid = self._open_staked_trade(gw)
        with patch.object(execute, "get_gateway", return_value=self._gateway()):
            reply = self._run(ADMIN, [str(DEMO), "confirm"])
        self.assertIn("ready for the next take", reply)
        self.assertEqual(live_ledger.get_trade(tid)["status"], "closed")
        account = pool.get_account(DEMO)
        self.assertAlmostEqual(float(account["cash_usd"]), 0.0, places=2)
        self.assertEqual(pool.strategy_subscriptions(DEMO), [])

    def test_bad_arguments_get_usage(self) -> None:
        reply = self._run(ADMIN, [])
        self.assertIn("Usage", reply)


if __name__ == "__main__":
    unittest.main()
