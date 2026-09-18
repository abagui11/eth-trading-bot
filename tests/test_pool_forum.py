"""Forum-topic routing for the hybrid Telegram UX.

Trade cards and research post once into the private forum group when the env
is set; unset env falls back to DM broadcast so nothing breaks on a box
without a forum. Pool mode keeps trade cards as personal DMs so each card
can show that user's Accept risk.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import access
import bot_config
import config
import notify
import paper
from models import Suggestion


class ForumTargetTests(unittest.TestCase):
    def test_unset_env_means_dm_mode(self) -> None:
        with patch.object(config, "POOL_FORUM_CHAT_ID", None):
            self.assertIsNone(notify.forum_trades_target())
            self.assertIsNone(notify.forum_research_target())

    def test_forum_targets_carry_their_thread_ids(self) -> None:
        with patch.object(config, "POOL_FORUM_CHAT_ID", -100123), patch.object(
            config, "POOL_FORUM_TRADES_THREAD_ID", 7
        ), patch.object(config, "POOL_FORUM_RESEARCH_THREAD_ID", 9):
            self.assertEqual(notify.forum_trades_target(), (-100123, 7))
            self.assertEqual(notify.forum_research_target(), (-100123, 9))

    def test_forum_without_topic_ids_posts_to_general(self) -> None:
        # A plain (non-forum) private group has no topics; thread_id None
        # must be passed through so sends land in the main chat.
        with patch.object(config, "POOL_FORUM_CHAT_ID", -100123), patch.object(
            config, "POOL_FORUM_TRADES_THREAD_ID", None
        ):
            self.assertEqual(notify.forum_trades_target(), (-100123, None))

    def test_pool_enabled_keeps_trade_cards_as_personal_dms(self) -> None:
        """Even with a forum configured, pool mode personalizes per user."""
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

        with patch.object(config, "POOL_FORUM_CHAT_ID", -100123), patch.object(
            config, "POOL_FORUM_TRADES_THREAD_ID", 7
        ), patch.object(bot_config, "POOL_ENABLED", True), patch.object(
            access, "strategy_recipient_ids", return_value=[1001, 1002]
        ), patch.object(
            notify, "send_suggestion_to_chat", side_effect=fake_send
        ), patch.object(config, "TELEGRAM_ADMIN_CHAT_ID", None), patch.object(
            config, "TELEGRAM_CHAT_ID", None
        ), patch.object(paper, "format_pnl_footer", return_value=""):
            reached = asyncio.run(
                notify.broadcast_to_subscribers(
                    AsyncMock(), sug, [], offer_id="c1"
                )
            )
        self.assertEqual(set(sent_chats), {1001, 1002})
        self.assertEqual(reached, {1001, 1002})
        self.assertNotIn(-100123, sent_chats)


if __name__ == "__main__":
    unittest.main()
