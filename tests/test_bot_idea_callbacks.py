"""The agent dispatcher handles the mill's idea:accept/reject callbacks.

The mill shares this bot's token and cannot poll getUpdates itself, so these
callbacks have to be serviced here.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _update_for(data: str, user_id: int = 42):
    query = MagicMock()
    query.data = data
    query.from_user = MagicMock(id=user_id, username="alice")
    query.message = MagicMock(chat_id=user_id)
    query.answer = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_user = query.from_user

    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    return update, context


class IdeaCallbackTests(unittest.TestCase):
    def _run(self, data: str, *, status: str = "recorded", user_id: int = 42):
        import bot as bot_mod

        update, context = _update_for(data, user_id=user_id)
        with (
            patch.object(bot_mod.access, "register_user"),
            patch.object(bot_mod.access, "is_allowed", return_value=True),
            patch.object(
                bot_mod.trade_ideas_bridge, "record_decision", return_value=status
            ) as record,
        ):
            asyncio.run(bot_mod.on_callback(update, context))
        return context, record

    def test_accept_records_and_confirms(self) -> None:
        context, record = self._run("idea:accept:7")
        record.assert_called_once_with(7, 42, "accept")
        context.bot.send_message.assert_awaited_once()
        text = context.bot.send_message.await_args.args[1]
        self.assertIn("Accepted idea #7", text)

    def test_reject_records(self) -> None:
        _, record = self._run("idea:reject:9")
        record.assert_called_once_with(9, 42, "reject")

    def test_duplicate_decision_message(self) -> None:
        context, _ = self._run("idea:accept:7", status="duplicate")
        text = context.bot.send_message.await_args.args[1]
        self.assertIn("already decided", text)

    def test_unavailable_bridge_message(self) -> None:
        context, _ = self._run("idea:accept:7", status="unavailable")
        text = context.bot.send_message.await_args.args[1]
        self.assertIn("unavailable", text)

    def test_malformed_callbacks_are_ignored(self) -> None:
        import bot as bot_mod

        for data in ("idea:accept", "idea:accept:abc", "idea:maybe:7", "idea:"):
            with self.subTest(data=data):
                update, context = _update_for(data)
                with (
                    patch.object(bot_mod.access, "register_user"),
                    patch.object(bot_mod.access, "is_allowed", return_value=True),
                    patch.object(
                        bot_mod.trade_ideas_bridge, "record_decision"
                    ) as record,
                ):
                    asyncio.run(bot_mod.on_callback(update, context))
                record.assert_not_called()
                context.bot.send_message.assert_not_awaited()

    def test_paywalled_user_never_reaches_the_bridge(self) -> None:
        import bot as bot_mod

        update, context = _update_for("idea:accept:7")
        update.callback_query.edit_message_text = AsyncMock()
        with (
            patch.object(bot_mod.access, "register_user"),
            patch.object(bot_mod.access, "is_allowed", return_value=False),
            patch.object(bot_mod.trade_ideas_bridge, "record_decision") as record,
        ):
            asyncio.run(bot_mod.on_callback(update, context))
        record.assert_not_called()

    def test_feed_callback_sends_magic_link(self) -> None:
        import bot as bot_mod

        update, context = _update_for("ui:feed")
        with (
            patch.object(bot_mod.access, "register_user"),
            patch.object(bot_mod.access, "is_allowed", return_value=True),
            patch.object(
                bot_mod.user_books,
                "feed_url",
                return_value="https://dash.example/feed?t=abc",
            ),
        ):
            asyncio.run(bot_mod.on_callback(update, context))
        context.bot.send_message.assert_awaited_once()
        text = context.bot.send_message.await_args.args[1]
        self.assertIn("https://dash.example/feed?t=abc", text)
        self.assertIn("Idea feed", text)


if __name__ == "__main__":
    unittest.main()
