"""Pool admins stay off the trade-idea stream.

Admit / deposit / wallet / treasury alerts still reach them. Trade cards,
z-moves, and research digests do not — use a separate tester id for that.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import access
import bot_config
import config
import notify
import paper
import pool
from models import Suggestion

ADMIN = 555001
TESTER = 777001


class AdminBroadcastExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "POOL_ADMIN_TELEGRAM_IDS", []),
            patch.object(config, "ALLOWED_TELEGRAM_IDS", []),
            patch.object(config, "INTERNAL_TELEGRAM_IDS", []),
            patch.object(config, "TELEGRAM_ADMIN_CHAT_ID", str(ADMIN)),
            patch.object(config, "TELEGRAM_CHAT_ID", None),
            patch.object(config, "PAYWALL_ENABLED", False),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        pool.init_db()
        pool.approve_user(ADMIN, admin_id=ADMIN, username="admin")
        pool.approve_user(TESTER, admin_id=ADMIN, username="ave")
        pool.subscribe_strategy(ADMIN, "ict")
        pool.subscribe_strategy(TESTER, "ict")

    def test_broadcast_recipients_exclude_approved_admin(self) -> None:
        self.assertEqual(access.broadcast_recipient_ids(), [TESTER])

    def test_strategy_recipients_exclude_admin_even_when_subscribed(self) -> None:
        self.assertEqual(access.strategy_recipient_ids("ict"), [TESTER])

    def test_internal_recipients_exclude_pool_admins(self) -> None:
        with patch.object(config, "INTERNAL_TELEGRAM_IDS", [ADMIN, TESTER]):
            self.assertEqual(access.internal_recipient_ids(), [TESTER])

    def test_internal_recipients_do_not_fall_back_to_admin_chat(self) -> None:
        with patch.object(config, "INTERNAL_TELEGRAM_IDS", []), patch.object(
            config, "ALLOWED_TELEGRAM_IDS", []
        ):
            self.assertEqual(access.internal_recipient_ids(), [])

    def test_trade_broadcast_does_not_copy_admin_chat(self) -> None:
        sent_chats: list[int] = []

        async def fake_send(bot, chat_id, *args, **kwargs):
            sent_chats.append(int(chat_id))

        sug = Suggestion(
            action="deriv_buy",
            size=0.1,
            entry=2000.0,
            stop_loss=1940.0,
            take_profits=[2060.0],
            risk_reward=1.5,
            rationale="t",
            product_id="ETH-USD",
        )
        with patch.object(notify, "send_suggestion_to_chat", side_effect=fake_send), \
                patch.object(paper, "format_pnl_footer", return_value=""):
            reached = asyncio.run(
                notify.broadcast_to_subscribers(AsyncMock(), sug, [], offer_id="c1")
            )
        self.assertEqual(set(sent_chats), {TESTER})
        self.assertEqual(reached, {TESTER})
        self.assertNotIn(ADMIN, sent_chats)

    def test_plain_broadcast_does_not_copy_admin_chat(self) -> None:
        sent_chats: list[int] = []

        class _Bot:
            def __init__(self, token: str) -> None:
                pass

            async def send_message(self, *, chat_id, text, **kwargs):
                sent_chats.append(int(chat_id))

        with patch.object(notify, "Bot", _Bot), patch.object(
            config, "POOL_FORUM_CHAT_ID", None
        ), patch.object(config, "TELEGRAM_BOT_TOKEN", "t"):
            asyncio.run(notify.broadcast_plain_text_async("z-move alert"))
        self.assertEqual(sent_chats, [TESTER])
        self.assertNotIn(ADMIN, sent_chats)

    def test_demo_recipients_exclude_admin(self) -> None:
        import demo_card

        self.assertEqual(demo_card.recipients(), [TESTER])

    def test_mill_stream_requires_subscription(self) -> None:
        """Nobody gets mill cards without /subscribe — allowlisted or not.

        The old allowlist carve-out kept env-allowlisted ids on every stream
        even after they unsubscribed, so the Trade Mill ([SPIKE]/[CASCADE])
        kept arriving. Subscription is the only gate now.
        """
        # TESTER is subscribed to ict only (setUp) — no mill recipients.
        self.assertEqual(access.strategy_recipient_ids("mill"), [])
        # Being env-allowlisted does not restore the stream.
        with patch.object(config, "ALLOWED_TELEGRAM_IDS", [TESTER]):
            self.assertEqual(access.strategy_recipient_ids("mill"), [])
        # An explicit subscribe is what turns it on; unsubscribe turns it off.
        pool.subscribe_strategy(TESTER, "mill")
        self.assertEqual(access.strategy_recipient_ids("mill"), [TESTER])
        pool.unsubscribe_strategy(TESTER, "mill")
        with patch.object(config, "ALLOWED_TELEGRAM_IDS", [TESTER]):
            self.assertEqual(access.strategy_recipient_ids("mill"), [])

    def test_seed_never_subscribes_anyone_to_mill(self) -> None:
        """The legacy startup seed grants ict only — mill is opt-in."""
        pool.seed_default_subscriptions()
        self.assertEqual(pool.strategy_subscriber_ids("mill"), set())
        self.assertIn("ict", pool.strategy_subscriptions(TESTER))


if __name__ == "__main__":
    unittest.main()
