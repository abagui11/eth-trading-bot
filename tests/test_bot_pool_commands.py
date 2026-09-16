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
import pool
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
            patch.object(bot_config, "POOL_MIN_DEPOSIT_USD", 500.0),
            patch.object(bot_config, "POOL_MIN_WITHDRAWAL_USD", 50.0),
            patch.object(bot_config, "POOL_MAX_WITHDRAWAL_USD", 2500.0),
            patch.object(bot_config, "POOL_WITHDRAWAL_FEE_RESERVE_USD", 3.0),
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

    def test_withdraw_confirms_and_cards_the_admin(self) -> None:
        """The debit happens before the reply, so a failure here means money
        held with nobody told. Both notifications must actually go out."""
        pool.approve_user(UID, admin_id=ADMIN)
        pool.register_wallet(UID, WALLET)
        pool.mark_wallet_verified(WALLET)
        pool.credit(UID, 1000.0, admin_id=ADMIN, ref="t")

        update, context = self._run(bot.cmd_withdraw, ["100"])

        self.assertIn("queued", self._texts(update))
        pending = pool.pending_withdrawals("requested")
        self.assertEqual(len(pending), 1)
        # The admin card is what makes the payout progress at all.
        self.assertEqual(context.bot.send_message.await_count, 1)
        self.assertEqual(context.bot.send_message.await_args.args[0], ADMIN)

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


if __name__ == "__main__":
    unittest.main()
