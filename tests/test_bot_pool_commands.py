"""The pool commands, driven through the real Telegram handlers.

Everything else tests `pool.*` directly, which is why a crash in the handler
layer survived: `/withdraw` called `_reply(..., markdown=True)` against a
two-argument `_reply` and raised TypeError. The severity is what makes these
worth having — on the success path the money is debited *before* the
confirmation is sent, so the failure meant funds held, the tester told
nothing, and the admin card never sent. It looked exactly like the bot eating
someone's money.

These call the handlers with a fake Update, so a signature or parse-mode
mistake fails here rather than in front of a tester.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import access
import bot
import bot_config
import config
import demo_card
import notify
import pool
import research
import telegram_ui
import trade_ideas_bridge
import user_books
from telegram.error import BadRequest

ADMIN = 555000
UID = 777001
ADDRESS = "0xDdA10FB6e6d726ae1cfB079CD79A4f0Ef7cAF240"
WALLET = "0x" + "cd" * 20


class PoolCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "POOL_DEPOSIT_ADDRESS", ADDRESS),
            patch.object(config, "POOL_DEPOSIT_CHAIN", "Ethereum mainnet"),
            patch.object(config, "POOL_FORUM_CHAT_ID", None),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
            # `admin_ids` merges the env list in so an operator can be added
            # without a deploy, which means patching the code config alone
            # leaves a real admin from a deployed `.env` in the recipients —
            # every "was the admin carded" assertion then sees two DMs.
            patch.object(config, "POOL_ADMIN_TELEGRAM_IDS", []),
            patch.object(bot_config, "POOL_MIN_DEPOSIT_USD", 500.0),
            patch.object(bot_config, "POOL_MIN_WITHDRAWAL_USD", 50.0),
            patch.object(bot_config, "POOL_MAX_WITHDRAWAL_USD", 2500.0),
            patch.object(bot_config, "POOL_WITHDRAWAL_FEE_RESERVE_USD", 3.0),
            # The quoted maximum nets off the daily caps, so leaving these to
            # whatever the host is configured for makes the withdraw-all
            # arithmetic below depend on the machine it runs on.
            patch.object(bot_config, "POOL_MAX_USER_DAILY_WITHDRAWAL_USD", 2500.0),
            patch.object(bot_config, "POOL_MAX_GLOBAL_DAILY_WITHDRAWAL_USD", 5000.0),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        pool.init_db()
        access.init_db()

    def _update(self, args=None):
        """A fake Update whose replies are captured rather than sent."""
        update = MagicMock()
        update.effective_user.id = UID
        update.effective_user.username = "tester"
        update.effective_user.full_name = "Tester"
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()
        context = MagicMock()
        context.args = args or []
        context.bot.send_message = AsyncMock()
        return update, context

    def _run(self, handler, args=None):
        update, context = self._update(args)
        asyncio.run(handler(update, context))
        return update, context

    def _texts(self, update) -> str:
        return "\n".join(
            str(call.args[0]) for call in update.message.reply_text.call_args_list
        )

    # -- the crash ---------------------------------------------------------

    def test_withdraw_overview_does_not_raise(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)
        pool.mark_wallet_verified(WALLET)
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref="t")

        update, _ = self._run(bot.cmd_withdraw)
        self.assertIn("Available now", self._texts(update))

    def _fund_for_withdrawal(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)
        pool.mark_wallet_verified(WALLET)
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref="t")

    def test_withdraw_confirms_and_cards_the_admin(self) -> None:
        """The debit happens before the reply, so a failure here means money
        held with nobody told. Both notifications must actually go out."""
        self._fund_for_withdrawal()
        with patch.object(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", False):
            update, context = self._run(bot.cmd_withdraw, ["100"])

        self.assertIn("queued", self._texts(update))
        pending = pool.pending_withdrawals("requested")
        self.assertEqual(len(pending), 1)
        # The admin card is what makes the payout progress at all.
        self.assertEqual(context.bot.send_message.await_count, 1)
        self.assertEqual(context.bot.send_message.await_args.args[0], ADMIN)
        self.assertIsNotNone(context.bot.send_message.await_args.kwargs["reply_markup"])

    def test_an_auto_approved_withdrawal_waits_on_nobody(self) -> None:
        """The admin was never deciding anything — every limit resolves at
        request time — so the tap only added however long it took them to look."""
        self._fund_for_withdrawal()
        with patch.object(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", True):
            update, context = self._run(bot.cmd_withdraw, ["100"])

        text = self._texts(update)
        self.assertIn("approved", text)
        self.assertNotIn("queued", text)
        # Tells them how long, so they are not left watching the screen.
        self.assertIn("five minutes", text)
        self.assertEqual(pool.pending_withdrawals("approved")[0]["telegram_id"], UID)

        # Admin is still told, but offered no buttons: Approve/Deny act only
        # on a `requested` row, so they would be controls that do nothing.
        self.assertEqual(context.bot.send_message.await_args.args[0], ADMIN)
        self.assertIsNone(context.bot.send_message.await_args.kwargs["reply_markup"])

    # -- /withdraw all -------------------------------------------------------

    def test_withdraw_all_takes_the_fee_aware_maximum(self) -> None:
        """The request is available minus the fee reserve, so it clears: asking
        for the raw $1,000 would be refused with the fee riding on top."""
        self._fund_for_withdrawal()
        with patch.object(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", True):
            update, _ = self._run(bot.cmd_withdraw, ["all"])

        pending = pool.pending_withdrawals("approved")
        self.assertEqual(len(pending), 1)
        # $1,000 available, $3 fee reserve: they are sent $997 and the full
        # $1,000 is held (unused reserve comes back after the real fee).
        self.assertAlmostEqual(float(pending[0]["amount_usd"]), 997.0, places=2)
        self.assertAlmostEqual(float(pending[0]["debited_usd"]), 1000.0, places=2)
        self.assertIn("997.00", self._texts(update))

    def test_withdraw_all_with_open_trades_names_what_stayed_behind(self) -> None:
        """Money committed to a trade cannot leave, and '/withdraw all' that
        silently keeps part of the balance looks like theft — the reply must
        say how much stayed and why, and still send the free part."""
        self._fund_for_withdrawal()
        with patch.object(bot_config, "POOL_RISK_PCT", 0.2), \
                patch.object(bot_config, "POOL_MIN_EQUITY_USD", 500.0):
            self.assertTrue(pool.record_intent("mill_1", UID)["ok"])

        with patch.object(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", True):
            update, _ = self._run(bot.cmd_withdraw, ["all"])

        # $200 reserved for the trade, $800 free, $3 fee reserve → $797 sent.
        pending = pool.pending_withdrawals("approved")
        self.assertEqual(len(pending), 1)
        self.assertAlmostEqual(float(pending[0]["amount_usd"]), 797.0, places=2)
        text = self._texts(update)
        self.assertIn("200.00", text)
        self.assertIn("open trades", text)

    def test_withdraw_all_with_everything_in_trades_sends_nothing(self) -> None:
        """When the free part is under the minimum, nothing goes out — and the
        reply says the money is in trades, not that it is gone."""
        self._fund_for_withdrawal()
        with patch.object(bot_config, "POOL_RISK_PCT", 0.97), \
                patch.object(bot_config, "POOL_MIN_EQUITY_USD", 500.0):
            self.assertTrue(pool.record_intent("mill_1", UID)["ok"])

        with patch.object(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", True):
            update, _ = self._run(bot.cmd_withdraw, ["all"])

        self.assertEqual(pool.pending_withdrawals("approved"), [])
        self.assertEqual(pool.pending_withdrawals("requested"), [])
        text = self._texts(update)
        self.assertIn("open trades", text)
        self.assertIn("970.00", text)  # names the committed amount

    def test_withdraw_all_with_an_empty_account_sends_nothing(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)
        pool.mark_wallet_verified(WALLET)

        update, _ = self._run(bot.cmd_withdraw, ["all"])
        self.assertEqual(pool.pending_withdrawals("approved"), [])
        self.assertEqual(pool.pending_withdrawals("requested"), [])
        self.assertIn("minimum", self._texts(update).lower())

    # -- /unsubscribe --------------------------------------------------------

    def _admin_update(self, args=None):
        update, context = self._update(args)
        update.effective_user.id = ADMIN
        return update, context

    def _tap_unsubscribe(self, choice: str, target: int = UID):
        update = MagicMock()
        query = update.callback_query
        query.from_user.id = ADMIN
        query.from_user.username = "admin"
        query.data = f"{telegram_ui.CB_POOL_UNSUB_PREFIX}{choice}:{target}"
        query.answer = AsyncMock()
        # The card is tapped in the admin's own DM, so the reply goes there.
        query.message.chat_id = ADMIN
        context = MagicMock()
        context.bot.send_message = AsyncMock()
        with patch.object(access, "is_allowed", return_value=True):
            asyncio.run(bot.on_callback(update, context))
        return context

    def test_unsubscribe_is_admin_only(self) -> None:
        """It takes an arbitrary id, so a tester must not be able to delete
        another tester's account."""
        self._fund_for_withdrawal()
        update, _ = self._run(bot.cmd_unsubscribe, [str(UID)])
        self.assertIn("restricted to pool admins", self._texts(update))
        self.assertIsNotNone(pool.get_account(UID))

        # Nor by tapping the confirm button directly.
        with patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", ()):
            with patch.object(config, "POOL_ADMIN_TELEGRAM_IDS", []):
                context = self._tap_unsubscribe("yes")
        context.bot.send_message.assert_not_awaited()
        self.assertIsNotNone(pool.get_account(UID))

    def test_the_command_only_previews_and_the_button_deletes(self) -> None:
        """A mistyped id deleting an account on the first Enter is not
        something anything downstream can undo."""
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)

        update, context = self._admin_update([str(UID)])
        asyncio.run(bot.cmd_unsubscribe(update, context))

        # Nothing gone yet, and the card offers the button that would do it.
        self.assertIsNotNone(pool.get_account(UID))
        self.assertTrue(pool.is_approved(UID))
        text = self._texts(update)
        self.assertIn("would delete", text)
        self.assertIn("cannot be undone", text)
        keyboard = update.message.reply_text.await_args.kwargs["reply_markup"]
        data = [b.callback_data
                for row in keyboard.inline_keyboard for b in row]
        self.assertIn(f"{telegram_ui.CB_POOL_UNSUB_PREFIX}yes:{UID}", data)

    def test_a_confirmed_removal_resets_onboarding_and_tells_them(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)

        context = self._tap_unsubscribe("yes")
        self.assertIsNone(pool.get_account(UID))
        self.assertFalse(pool.is_approved(UID))
        self.assertEqual(pool.request_access(UID, "tester"), "new")

        # The admin is told, and so is the person whose account it was.
        targets = [c.args[0] for c in context.bot.send_message.await_args_list]
        self.assertIn(ADMIN, targets)
        self.assertIn(UID, targets)

    def test_cancel_leaves_the_account_alone(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        context = self._tap_unsubscribe("no")
        self.assertIsNotNone(pool.get_account(UID))
        self.assertIn("untouched", str(context.bot.send_message.await_args.args[1]))

    def test_a_funded_account_is_refused_with_the_reason(self) -> None:
        self._fund_for_withdrawal()
        context = self._tap_unsubscribe("yes")
        self.assertIsNotNone(pool.get_account(UID))
        reply = str(context.bot.send_message.await_args.args[1])
        self.assertIn("/withdraw all", reply)

    def test_a_markdown_failure_still_delivers_the_text(self) -> None:
        """Telegram refuses a whole message it cannot parse. On this path that
        would be silence after a debit, so it must fall back to plain text."""
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)
        pool.mark_wallet_verified(WALLET)
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref="t")

        update, context = self._update(["100"])
        update.message.reply_text = AsyncMock(
            side_effect=[BadRequest("can't parse entities"), None]
        )
        asyncio.run(bot.cmd_withdraw(update, context))

        self.assertEqual(update.message.reply_text.await_count, 2)
        # The retry carries no parse_mode, which is what makes it succeed.
        self.assertNotIn("parse_mode", update.message.reply_text.await_args.kwargs)

    # -- the rest of the onboarding path -----------------------------------

    def test_first_contact_files_a_request_and_pings_admins(self) -> None:
        update, context = self._run(bot.cmd_start)
        self.assertIn("invite-only", self._texts(update))
        self.assertEqual(context.bot.send_message.await_args.args[0], ADMIN)

    def test_welcome_after_admission(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        update, _ = self._run(bot.cmd_start)
        self.assertIn("you're in", self._texts(update))

    def test_deposit_asks_for_a_wallet_first(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        update, _ = self._run(bot.cmd_deposit)
        text = self._texts(update)
        self.assertIn("/wallet", text)
        # The deposit address must NOT be shown yet: a transfer sent before
        # registration cannot be attributed to anyone.
        self.assertNotIn(ADDRESS, text)

    def test_deposit_shows_the_address_once_a_wallet_exists(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)
        update, _ = self._run(bot.cmd_deposit)
        text = self._texts(update)
        self.assertIn(ADDRESS, text)
        self.assertIn("Ethereum mainnet", text)

    def test_wallet_registration_and_status(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        self._run(bot.cmd_wallet, [WALLET])
        self.assertIsNotNone(pool.get_wallet(UID))

        update, _ = self._run(bot.cmd_wallet)
        self.assertIn("not yet confirmed", self._texts(update))

        pool.mark_wallet_verified(WALLET)
        update, _ = self._run(bot.cmd_wallet)
        text = self._texts(update)
        self.assertIn("Confirmed", text)
        self.assertIn("Withdrawals are open", text)

    def test_an_unverified_wallet_cannot_withdraw(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref="t")
        update, _ = self._run(bot.cmd_withdraw, ["100"])
        self.assertEqual(pool.pending_withdrawals("requested"), [])
        self.assertTrue(self._texts(update))

    def test_portfolio_renders(self) -> None:
        pool.approve_user(UID, admin_id=ADMIN)
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref="t")
        with patch("research.get_spot_prices", return_value={}):
            update, _ = self._run(bot.cmd_portfolio)
        self.assertIn("1,000.00", self._texts(update))


class DemoCardTests(unittest.TestCase):
    """The demo card, whose whole value depends on Accept being both real and
    incapable of trading.

    Real, because a demo that quotes a size the live path would not produce is
    a demo of something that does not exist. Incapable, because it will be
    pressed on camera with real money in the account. The second property is
    structural -- every executor resolves intents by ref, and a `demo_` ref
    matches no order -- so the test that matters most is that a demo intent
    contributes nothing to a real one.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
            patch.object(bot_config, "POOL_RISK_PCT", 0.007),
            patch.object(bot_config, "POOL_MIN_EQUITY_USD", 500.0),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        pool.init_db()
        access.init_db()
        pool.approve_user(UID, admin_id=ADMIN)
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref="fund")

    def _press(self, choice: str, token: str = "abc123"):
        update = MagicMock()
        query = update.callback_query
        query.from_user.id = UID
        query.from_user.username = "tester"
        query.data = f"{telegram_ui.CB_POOL_DEMO_PREFIX}{choice}:{token}"
        query.answer = AsyncMock()
        context = MagicMock()
        context.bot.send_message = AsyncMock()
        with patch.object(access, "is_allowed", return_value=True):
            asyncio.run(bot.on_callback(update, context))
        return context

    def test_accept_reserves_the_real_budget(self) -> None:
        context = self._press("yes")
        reply = str(context.bot.send_message.await_args.args[1])
        self.assertIn("You're in if it fills", reply)

        intents = pool.pending_intents("demo_abc123")
        self.assertEqual(len(intents), 1)
        # 0.7% of $1,000 available — the live rule, not a demo constant.
        self.assertAlmostEqual(float(intents[0]["risk_usd"]), 7.0, places=2)
        self.assertAlmostEqual(
            float(pool.get_account(UID)["reserved_usd"]), 7.0, places=2
        )

    def test_a_demo_intent_cannot_join_a_real_order(self) -> None:
        """The property that makes this safe to press with real money."""
        self._press("yes")
        extra, intents = pool.extra_contracts_for(
            "20260916T150000Z", risk_per_unit=10.0, floor=0.01
        )
        self.assertEqual(extra, 0.0)
        self.assertEqual(intents, [])

        opened = pool.open_stakes(
            1, "20260916T150000Z", fill_qty=0.01, fill_price=100_000.0,
            risk_per_unit=10.0, house_risk_usd=14.0,
        )
        self.assertEqual(opened, [])
        self.assertEqual(pool.open_stakes_for(1), [])

    def test_the_reserve_comes_back_on_the_next_sweep(self) -> None:
        """No order exists, so the ref is never active and the real
        stale-intent path returns the money with the real message."""
        self._press("yes")
        released = pool.expire_stale_intents(set())
        self.assertEqual(len(released), 1)
        self.assertEqual(str(released[0]["ref"]), "demo_abc123")
        self.assertEqual(float(pool.get_account(UID)["reserved_usd"]), 0.0)
        self.assertEqual(float(pool.get_account(UID)["cash_usd"]), 1000.0)

    def test_a_real_ref_is_not_released_by_the_demo_sweep(self) -> None:
        """Guard against the demo path teaching the sweep to be too eager."""
        pool.record_intent("20260916T150000Z", UID)
        released = pool.expire_stale_intents({"20260916T150000Z"})
        self.assertEqual(released, [])
        self.assertGreater(float(pool.get_account(UID)["reserved_usd"]), 0.0)

    def test_reject_reserves_nothing(self) -> None:
        context = self._press("no")
        self.assertIn("Skipped", str(context.bot.send_message.await_args.args[1]))
        self.assertEqual(pool.pending_intents("demo_abc123"), [])
        self.assertEqual(float(pool.get_account(UID)["reserved_usd"]), 0.0)

    def test_an_unfunded_account_is_told_to_deposit(self) -> None:
        pool.debit(UID, 1000.0, admin_id=ADMIN, ref="drain")
        context = self._press("yes")
        self.assertIn("/deposit", str(context.bot.send_message.await_args.args[1]))
        self.assertEqual(pool.pending_intents("demo_abc123"), [])

    def test_malformed_demo_data_is_ignored(self) -> None:
        context = self._press("maybe")
        context.bot.send_message.assert_not_awaited()


class PoolMillAcceptTests(unittest.TestCase):
    """A funded tester's Accept on a mill card.

    Two properties, both learned the hard way. It must *fill* rather than
    reserve-and-hope, because the house filled 62 of 544 recent ideas and the
    common outcome was "you're in if it fills" followed by nothing. And when it
    cannot fill, the money must come back **now**, with the real reason, rather
    than sitting pending until a sweep notices up to two hours later.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
            patch.object(bot_config, "POOL_RISK_PCT", 0.007),
            patch.object(bot_config, "POOL_MIN_EQUITY_USD", 500.0),
            patch.object(bot_config, "LIVE_MILL_ANY_ACCEPT_FILLS", True),
            patch.object(bot_config, "LIVE_MILL_FILL_TELEGRAM_IDS", (ADMIN,)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        pool.init_db()
        access.init_db()
        pool.approve_user(UID, admin_id=ADMIN)
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref="fund")

    def _accept(self, verdict: dict):
        with patch.object(trade_ideas_bridge, "idea_pool_open", return_value=True), \
                patch.object(trade_ideas_bridge, "request_manual_fill",
                             return_value=verdict) as fill:
            reply = bot._pool_mill_accept(1025, UID)
        return reply, fill

    def test_an_accept_fills_there_and_then(self) -> None:
        reply, fill = self._accept({
            "executed": True,
            "result": {"trade_id": 7, "fill": 2380.0},
        })
        fill.assert_called_once_with(1025, UID)
        self.assertIn("You're in", reply)

    def test_the_intent_exists_before_the_fill_is_attempted(self) -> None:
        """Ordering is load-bearing: the pooled aggregation in
        execute_mill_idea resolves intents by ref, so an intent recorded after
        the fill would leave the tester out of the order they just joined."""
        seen: dict = {}

        def _capture(idea_id, user_id):
            seen["pending"] = pool.pending_intents(f"mill_{idea_id}")
            return {"executed": True, "result": {"trade_id": 7, "fill": 2380.0}}

        with patch.object(trade_ideas_bridge, "idea_pool_open", return_value=True), \
                patch.object(trade_ideas_bridge, "request_manual_fill", _capture):
            bot._pool_mill_accept(1025, UID)

        self.assertEqual(len(seen["pending"]), 1)
        self.assertEqual(int(seen["pending"][0]["telegram_id"]), UID)

    def test_a_refusal_returns_the_money_immediately(self) -> None:
        reply, _ = self._accept({"executed": False, "skip_reason": "rr_collapsed"})

        self.assertEqual(pool.pending_intents("mill_1025"), [])
        self.assertEqual(float(pool.get_account(UID)["reserved_usd"]), 0.0)
        self.assertEqual(float(pool.get_account(UID)["cash_usd"]), 1000.0)
        self.assertIn("Not filled", reply)
        self.assertIn("nothing was risked", reply)

    def test_a_refusal_says_why_in_words_a_tester_can_act_on(self) -> None:
        reply, _ = self._accept({"executed": False, "skip_reason": "sleeve_full"})
        self.assertIn("maximum number of trades", reply)
        self.assertNotIn("sleeve_full", reply)

    def test_an_unknown_skip_reason_still_returns_the_money(self) -> None:
        reply, _ = self._accept({"executed": False, "skip_reason": "wat"})
        self.assertEqual(float(pool.get_account(UID)["reserved_usd"]), 0.0)
        self.assertIn("no longer qualified", reply)

    def test_a_crash_in_the_fill_does_not_strand_the_reserve_silently(self) -> None:
        """Fail-soft, but the tester is told they are still pending rather
        than told they are in."""
        with patch.object(trade_ideas_bridge, "idea_pool_open", return_value=True), \
                patch.object(trade_ideas_bridge, "request_manual_fill",
                             side_effect=RuntimeError("coinbase 503")):
            reply = bot._pool_mill_accept(1025, UID)
        self.assertIn("You're in if it fills", reply)
        self.assertEqual(len(pool.pending_intents("mill_1025")), 1)

    def test_an_unfunded_tester_cannot_trigger_a_house_fill(self) -> None:
        """The flag deploys house money, so it must require a real stake."""
        pool.approve_user(999123, admin_id=ADMIN)
        self.assertFalse(trade_ideas_bridge.may_fill(999123))

    def test_the_flag_off_restores_the_operator_allowlist(self) -> None:
        with patch.object(bot_config, "LIVE_MILL_ANY_ACCEPT_FILLS", False):
            self.assertFalse(trade_ideas_bridge.may_fill(UID))
            self.assertTrue(trade_ideas_bridge.may_fill(ADMIN))


class LiveCardTests(unittest.TestCase):
    """`/democard real` sends a card that can actually fill.

    A demo card never fills — its ref matches no executor — so "accept it on
    camera and watch it go in" needs a real mill card, with the real Accept
    callback and a banner that says so.
    """

    def test_a_fillable_idea_is_preferred_over_a_refusable_one(self) -> None:
        rows = [
            {"id": 30, "would_fill": False, "preview": {"skip_reason": "expired"}},
            {"id": 29, "would_fill": True, "preview": {"born_rr": 1.4}},
            {"id": 28, "would_fill": True, "preview": {"born_rr": 2.0}},
        ]
        with patch.object(trade_ideas_bridge, "fillable_ideas", return_value=rows):
            picked = demo_card.pick_fillable_idea(UID)
        self.assertEqual(picked["id"], 29)  # newest that would fill

    def test_no_card_is_sent_when_nothing_would_fill(self) -> None:
        """Better to say so than to hand someone a card that will refuse."""
        rows = [{"id": 30, "would_fill": False, "preview": {}}]
        with patch.object(trade_ideas_bridge, "fillable_ideas", return_value=rows), \
                patch.object(bot_config, "POOL_ENABLED", True), \
                patch.object(pool, "is_approved", return_value=True):
            result = demo_card.send(UID, live_idea=True)
        self.assertEqual(result["reason"], "nothing_fillable")

    def test_the_live_card_carries_the_real_accept_and_says_it_is_real(self) -> None:
        idea = {"id": 29, "would_fill": True, "preview": {"born_rr": 1.4}}
        row = {
            "id": 29, "product_id": "ETH-USD", "direction": "short",
            "entry": 2400.0, "stop_loss": 2450.0,
            "take_profits_json": "[2300.0]", "title": "ETH short",
        }
        captured: dict = {}

        def _capture(uid, text, keyboard):
            captured["text"] = text
            captured["keyboard"] = keyboard
            return True

        with patch.object(trade_ideas_bridge, "fillable_ideas", return_value=[idea]), \
                patch.object(trade_ideas_bridge, "_idea_row", return_value=row), \
                patch.object(bot_config, "POOL_ENABLED", True), \
                patch.object(pool, "is_approved", return_value=True), \
                patch.object(pool, "prospective_accept",
                             return_value={"ok": True, "risk_usd": 3.5,
                                           "risk_pct": 0.007,
                                           "available_usd": 500.0,
                                           "notional_usd": 168.0}), \
                patch.object(notify, "send_pool_dm_with_keyboard", _capture):
            result = demo_card.send(UID, live_idea=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["live"])
        self.assertEqual(result["ref"], "mill_29")
        # Levels are the idea's own, not invented.
        self.assertEqual(result["entry"], 2400.0)
        self.assertEqual(result["stop_loss"], 2450.0)

        # A card that spends money must not be labelled a demo.
        self.assertIn("LIVE CARD", captured["text"])
        self.assertNotIn("DEMO CARD", captured["text"])

        data = [b.callback_data
                for row_ in captured["keyboard"].inline_keyboard for b in row_]
        self.assertIn("idea:accept:29", data)
        self.assertFalse(
            any(d.startswith(telegram_ui.CB_POOL_DEMO_PREFIX) for d in data),
            "a live card must not carry the demo callback",
        )

    def test_real_does_not_hijack_the_existing_live_argument(self) -> None:
        """`/democard live` already means "mirror an open position"."""
        mirror = demo_card.parse_args(["live"], default_id=1)
        self.assertTrue(mirror["mirror"])
        self.assertFalse(mirror["live_idea"])

        real = demo_card.parse_args(["real"], default_id=1)
        self.assertTrue(real["live_idea"])
        self.assertFalse(real["mirror"])


class LegacyPaperBookTests(unittest.TestCase):
    """The demo paper book is off for live accounts.

    A funded tester tapped "Join now" on a missed-connection DM, got a refusal
    carrying the old main keyboard, tapped "Open account" on it, and ended up
    with a $2,500 demo book sitting beside their real balance. Two books, one
    imaginary, shown to someone checking whether their money is safe.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        pool.init_db()
        access.init_db()
        pool.approve_user(UID, admin_id=ADMIN)

    def _tap(self, data: str, uid: int = UID):
        update = MagicMock()
        query = update.callback_query
        query.from_user.id = uid
        query.from_user.username = "tester"
        query.data = data
        query.answer = AsyncMock()
        context = MagicMock()
        context.bot.send_message = AsyncMock()
        with patch.object(access, "is_allowed", return_value=True):
            asyncio.run(bot.on_callback(update, context))
        return context

    def test_every_demo_book_button_is_refused(self) -> None:
        for data in (
            telegram_ui.CB_OPEN,
            telegram_ui.CB_METRICS,
            telegram_ui.CB_MY_BOOK,
            f"{telegram_ui.CB_OPEN_SIZE_PREFIX}2500",
            f"{telegram_ui.CB_TRADE_JOIN_PREFIX}offer-1",
            f"{telegram_ui.CB_TRADE_SKIP_PREFIX}offer-1",
        ):
            with self.subTest(data=data), \
                    patch.object(user_books, "open_paper_account") as opened, \
                    patch.object(user_books, "late_join_offer") as joined:
                context = self._tap(data)
                opened.assert_not_called()
                joined.assert_not_called()
                reply = str(context.bot.send_message.await_args.args[1])
                self.assertIn("old demo book", reply)
                self.assertIn("/portfolio", reply)

    def test_the_size_picker_cannot_open_a_2500_book(self) -> None:
        """The exact tap that created one."""
        with patch.object(user_books, "open_paper_account") as opened:
            self._tap(f"{telegram_ui.CB_OPEN_SIZE_PREFIX}2500")
        opened.assert_not_called()

    def test_a_non_pool_user_keeps_the_demo_book(self) -> None:
        """Nothing here should break the pre-pool product."""
        with patch.object(bot_config, "POOL_ENABLED", False), \
                patch.object(user_books, "has_account", return_value=False):
            context = self._tap(telegram_ui.CB_OPEN, uid=424242)
        reply = str(context.bot.send_message.await_args.args[1])
        self.assertNotIn("old demo book", reply)

    def test_live_accounts_are_not_sent_missed_connection_invites(self) -> None:
        """'Join now' enters at the mark against the original stop — a chase.
        Fine with imaginary money, not something to offer a real balance."""
        import notify

        async def _run():
            with patch.object(notify, "Bot") as bot_cls, \
                    patch.object(user_books, "mark_missed_connection_sent") as marked:
                bot_cls.return_value.send_message = AsyncMock()
                await notify.send_missed_connection_async({
                    "offer_id": "offer-1",
                    "telegram_ids": [UID],
                    "r_multiple": 1.2, "spot": 2400.0,
                    "product_id": "ETH-USD",
                })
                return bot_cls.return_value.send_message, marked

        sent, marked = asyncio.run(_run())
        sent.assert_not_awaited()
        # Still closed out, so the sweep does not retry it every cycle.
        marked.assert_called_once_with("offer-1")


class SendDemoCardTests(unittest.TestCase):
    """`/democard` — the send side.

    It gets typed on camera, so the arguments have to be forgiving; and it can
    address any telegram id, so it has to be admin-only.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
            patch.object(bot_config, "POOL_RISK_PCT", 0.007),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        pool.init_db()
        access.init_db()
        pool.approve_user(ADMIN, admin_id=ADMIN)
        pool.approve_user(UID, admin_id=ADMIN)

    def _run(self, caller: int, args=None):
        update = MagicMock()
        update.effective_user.id = caller
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = args or []
        with patch.object(research, "get_spot_price", return_value=75_000.0), \
                patch.object(notify, "send_pool_dm_with_keyboard",
                             return_value=True) as sent:
            asyncio.run(bot.cmd_democard(update, context))
        replies = "\n".join(
            str(c.args[0]) for c in update.message.reply_text.call_args_list
        )
        return replies, sent

    # -- access ------------------------------------------------------------

    def test_a_tester_cannot_send_cards_to_anyone(self) -> None:
        """It takes an arbitrary telegram id, so this is the guard that
        matters: a tester must not be able to card another tester."""
        replies, sent = self._run(UID, [str(ADMIN)])
        self.assertEqual(replies, "")
        sent.assert_not_called()

    # -- arguments ---------------------------------------------------------

    def test_bare_command_cards_the_admin_themselves(self) -> None:
        opts = demo_card.parse_args([], default_id=ADMIN)
        self.assertEqual(opts["telegram_id"], ADMIN)
        self.assertEqual(opts["product"], "BTC-USD")
        self.assertEqual(opts["side"], "buy")
        self.assertFalse(opts["mirror"])

    def test_arguments_are_order_insensitive(self) -> None:
        """Typed live, so 'eth short 777001' must work as well as the
        documented order."""
        canonical = demo_card.parse_args(
            [str(UID), "eth", "short"], default_id=ADMIN)
        for order in (["eth", "short", str(UID)],
                      ["short", str(UID), "ETH"],
                      ["--eth", str(UID), "sell"]):
            self.assertEqual(
                demo_card.parse_args(order, default_id=ADMIN), canonical, order
            )
        self.assertEqual(canonical["product"], "ETH-USD")
        self.assertEqual(canonical["side"], "sell")

    def test_junk_arguments_fall_back_rather_than_raise(self) -> None:
        opts = demo_card.parse_args(["banana", "42"], default_id=ADMIN)
        self.assertEqual(opts["telegram_id"], ADMIN)
        self.assertEqual(opts["product"], "BTC-USD")

    # -- what gets sent ----------------------------------------------------

    def test_a_funded_target_is_quoted_the_live_size(self) -> None:
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref="fund")
        replies, sent = self._run(ADMIN, [str(UID)])

        sent.assert_called_once()
        body = str(sent.call_args.args[1])
        self.assertIn("DEMO CARD", body)
        # 0.7% of $1,000, straight off the live rule.
        self.assertIn("7.00", replies)

    def test_an_unfunded_target_is_flagged_to_the_sender(self) -> None:
        """Otherwise it is discovered on playback."""
        replies, sent = self._run(ADMIN, [str(UID)])
        sent.assert_called_once()
        self.assertIn("/deposit", replies)

    def test_levels_track_spot_and_respect_side(self) -> None:
        long = demo_card.build_suggestion("BTC-USD", "buy", 75_000.0)
        self.assertLess(long.stop_loss, long.entry)
        self.assertGreater(long.take_profits[0], long.entry)
        self.assertLess(abs(long.entry - 75_000.0) / 75_000.0, 0.01)

        short = demo_card.build_suggestion("BTC-USD", "sell", 75_000.0)
        self.assertGreater(short.stop_loss, short.entry)
        self.assertLess(short.take_profits[0], short.entry)

    def test_the_ref_is_demo_prefixed(self) -> None:
        """The prefix is the whole safety property — it matches no order."""
        with patch.object(research, "get_spot_price", return_value=75_000.0), \
                patch.object(notify, "send_pool_dm_with_keyboard",
                             return_value=True):
            result = demo_card.send(UID)
        self.assertTrue(str(result["ref"]).startswith("demo_"))

    # -- mirroring a real position -----------------------------------------

    def test_live_is_recognised_in_any_position(self) -> None:
        for args in (["live"], ["live", str(UID)], ["mill"], ["85"]):
            self.assertTrue(
                demo_card.parse_args(args, default_id=ADMIN)["mirror"], args
            )
        self.assertEqual(
            demo_card.parse_args(["mill"], default_id=ADMIN)["source"], "mill"
        )

    def test_a_short_number_is_a_trade_id_not_a_telegram_id(self) -> None:
        """`85` and `8708390551` must not be confused for each other."""
        opts = demo_card.parse_args(["85", "8708390551"], default_id=ADMIN)
        self.assertEqual(opts["trade_id"], 85)
        self.assertEqual(opts["telegram_id"], 8708390551)

    def test_a_mirror_copies_the_real_levels_untouched(self) -> None:
        """The whole point: nothing about the setup is invented."""
        trade = {
            "id": 85, "source": "mill", "cycle_id": "mill_969",
            "product_id": "ETH-USD", "side": "short", "entry": 2382.5,
            "stop_loss": 2400.0, "initial_stop_loss": 2445.73,
            "take_profits_json": "[2344.91]",
            "plan_take_profits_json": "[2344.91, 2300.0]",
            "status": "open",
        }
        with patch.object(demo_card, "_original_rationale", return_value=None):
            s = demo_card.build_from_trade(trade)

        self.assertEqual(s.action, "spot_sell")
        self.assertEqual(s.entry, 2382.5)
        self.assertEqual(s.product_id, "ETH-USD")
        # The original plan, not the shrunken list left after a target fills.
        self.assertEqual(s.take_profits, [2344.91, 2300.0])
        # The stop the trade was sized against, not a trailed one.
        self.assertEqual(s.stop_loss, 2445.73)
        self.assertIn("#85", s.rationale)

    def test_a_mirror_quotes_the_size_that_trade_would_have_taken(self) -> None:
        pool.credit(UID, 510.0, admin_id=ADMIN, ref="fund")
        trade = {
            "id": 85, "source": "mill", "cycle_id": "mill_969",
            "product_id": "ETH-USD", "side": "short", "entry": 2382.5,
            "stop_loss": 2445.73, "initial_stop_loss": 2445.73,
            "plan_take_profits_json": "[2344.91]", "status": "open",
        }
        with patch.object(demo_card, "pick_trade", return_value=trade), \
                patch.object(demo_card, "_original_rationale", return_value=None), \
                patch.object(research, "get_spot_price", return_value=2400.0), \
                patch.object(notify, "send_pool_dm_with_keyboard",
                             return_value=True) as sent:
            result = demo_card.send(UID, mirror=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["mirrored_trade_id"], 85)
        self.assertEqual(result["product"], "ETH-USD")
        # 0.7% of $510 at the real stop distance — the live rule on real levels.
        self.assertAlmostEqual(result["risk_usd"], 3.57, places=2)
        self.assertIn("mirrors a real open position", str(sent.call_args.args[1]))

    def test_a_stale_mirrored_entry_is_flagged_before_filming(self) -> None:
        trade = {
            "id": 85, "source": "mill", "cycle_id": "mill_969",
            "product_id": "ETH-USD", "side": "short", "entry": 2382.5,
            "stop_loss": 2445.73, "initial_stop_loss": 2445.73,
            "plan_take_profits_json": "[2344.91]", "status": "open",
        }
        update = MagicMock()
        update.effective_user.id = ADMIN
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["live"]
        with patch.object(demo_card, "pick_trade", return_value=trade), \
                patch.object(demo_card, "_original_rationale", return_value=None), \
                patch.object(research, "get_spot_price", return_value=2600.0), \
                patch.object(notify, "send_pool_dm_with_keyboard",
                             return_value=True):
            asyncio.run(bot.cmd_democard(update, context))

        replies = "\n".join(
            str(c.args[0]) for c in update.message.reply_text.call_args_list
        )
        self.assertIn("mirrors live mill #85", replies)
        self.assertIn("off that entry", replies)

    def test_nothing_open_says_so_rather_than_inventing_a_trade(self) -> None:
        update = MagicMock()
        update.effective_user.id = ADMIN
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["live"]
        with patch.object(demo_card, "pick_trade", return_value=None), \
                patch.object(notify, "send_pool_dm_with_keyboard",
                             return_value=True) as sent:
            asyncio.run(bot.cmd_democard(update, context))

        sent.assert_not_called()
        replies = "\n".join(
            str(c.args[0]) for c in update.message.reply_text.call_args_list
        )
        self.assertIn("nothing is open to mirror", replies)

    def test_a_closed_trade_is_not_mirrored(self) -> None:
        with patch("live_ledger.get_trade",
                   return_value={"id": 85, "status": "closed"}):
            self.assertIsNone(demo_card.pick_trade(85))

    # -- broadcast ---------------------------------------------------------

    def test_all_sends_one_card_per_account_sized_individually(self) -> None:
        """A shared render would show every tester the same number; the size
        line is personal, so each card is built for its own account."""
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref="fund")
        update = MagicMock()
        update.effective_user.id = ADMIN
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["all"]
        with patch.object(research, "get_spot_price", return_value=75_000.0), \
                patch.object(notify, "send_pool_dm_with_keyboard",
                             return_value=True) as sent:
            asyncio.run(bot.cmd_democard(update, context))

        # ADMIN is funded-less, UID has $1,000: both approved, both carded.
        self.assertEqual(sent.call_count, 2)
        self.assertEqual({c.args[0] for c in sent.call_args_list}, {ADMIN, UID})
        replies = "\n".join(
            str(c.args[0]) for c in update.message.reply_text.call_args_list
        )
        self.assertIn("2 account(s)", replies)
        self.assertIn("1 of them are funded", replies)

    def test_a_tester_cannot_broadcast(self) -> None:
        update = MagicMock()
        update.effective_user.id = UID
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["all"]
        with patch.object(notify, "send_pool_dm_with_keyboard",
                          return_value=True) as sent:
            asyncio.run(bot.cmd_democard(update, context))
        sent.assert_not_called()

    def test_an_unreachable_account_is_named_not_silently_dropped(self) -> None:
        update = MagicMock()
        update.effective_user.id = ADMIN
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["all"]
        with patch.object(research, "get_spot_price", return_value=75_000.0), \
                patch.object(notify, "send_pool_dm_with_keyboard",
                             side_effect=[True, False]):
            asyncio.run(bot.cmd_democard(update, context))
        replies = "\n".join(
            str(c.args[0]) for c in update.message.reply_text.call_args_list
        )
        self.assertIn("Could not reach", replies)

    def test_an_unapproved_target_is_refused(self) -> None:
        with patch.object(research, "get_spot_price", return_value=75_000.0), \
                patch.object(notify, "send_pool_dm_with_keyboard",
                             return_value=True) as sent:
            result = demo_card.send(999999)
        self.assertFalse(result["ok"])
        sent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
