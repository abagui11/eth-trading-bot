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

    def _run_as_operator(self, verdict: dict, *, idea: int = 7):
        """Accept from a LIVE_MILL_FILL_TELEGRAM_IDS operator (real allowlist)."""
        import bot as bot_mod
        import bot_config

        operator = bot_config.LIVE_MILL_FILL_TELEGRAM_IDS[0]
        update, context = _update_for(f"idea:accept:{idea}", user_id=operator)
        with (
            patch.object(bot_mod.access, "register_user"),
            patch.object(bot_mod.access, "is_allowed", return_value=True),
            patch.object(
                bot_mod.trade_ideas_bridge, "record_decision", return_value="recorded"
            ),
            patch.object(
                bot_mod.trade_ideas_bridge,
                "request_manual_fill",
                return_value=verdict,
            ) as fill,
        ):
            asyncio.run(bot_mod.on_callback(update, context))
        texts = [c.args[1] for c in context.bot.send_message.await_args_list]
        return fill, texts, operator

    def test_operator_accept_takes_a_live_clip(self) -> None:
        fill, texts, operator = self._run_as_operator(
            {
                "executed": True,
                "capacity": {"open": 1, "max_open": 3},
                "result": {"mode": "live", "qty": 0.1, "notional_usd": 300.0},
            }
        )
        fill.assert_called_once_with(7, operator)
        self.assertIn("Accepted idea #7", texts[0])
        self.assertIn("Live clip placed", texts[1])
        self.assertIn("sleeve 1/3", texts[1])

    def test_operator_accept_at_max_reports_too_many(self) -> None:
        _, texts, _ = self._run_as_operator(
            {
                "executed": False,
                "skip_reason": "sleeve_full",
                "capacity": {
                    "open": 3,
                    "max_open": 3,
                    "open_trades": [
                        {
                            "id": 1,
                            "product_id": "ETH-USD",
                            "side": "long",
                            "entry": 3000.0,
                            "fill_type": "auto",
                        }
                    ],
                },
            }
        )
        self.assertIn("Too many trades open", texts[1])
        self.assertIn("3/3", texts[1])
        self.assertIn("ETH-USD", texts[1])

    def test_ordinary_subscriber_accept_never_fills(self) -> None:
        import bot as bot_mod

        update, context = _update_for("idea:accept:7", user_id=42)
        with (
            patch.object(bot_mod.access, "register_user"),
            patch.object(bot_mod.access, "is_allowed", return_value=True),
            patch.object(
                bot_mod.trade_ideas_bridge, "record_decision", return_value="recorded"
            ),
            patch.object(
                bot_mod.trade_ideas_bridge, "request_manual_fill"
            ) as fill,
        ):
            asyncio.run(bot_mod.on_callback(update, context))
        fill.assert_not_called()

    def test_duplicate_accept_does_not_refill(self) -> None:
        """A second Accept on the same idea must not take a second clip."""
        import bot as bot_mod
        import bot_config

        operator = bot_config.LIVE_MILL_FILL_TELEGRAM_IDS[0]
        update, context = _update_for("idea:accept:7", user_id=operator)
        with (
            patch.object(bot_mod.access, "register_user"),
            patch.object(bot_mod.access, "is_allowed", return_value=True),
            patch.object(
                bot_mod.trade_ideas_bridge, "record_decision", return_value="duplicate"
            ),
            patch.object(
                bot_mod.trade_ideas_bridge, "request_manual_fill"
            ) as fill,
        ):
            asyncio.run(bot_mod.on_callback(update, context))
        fill.assert_not_called()

    def test_fill_failure_never_breaks_the_accept(self) -> None:
        import bot as bot_mod
        import bot_config

        operator = bot_config.LIVE_MILL_FILL_TELEGRAM_IDS[0]
        update, context = _update_for("idea:accept:7", user_id=operator)
        with (
            patch.object(bot_mod.access, "register_user"),
            patch.object(bot_mod.access, "is_allowed", return_value=True),
            patch.object(
                bot_mod.trade_ideas_bridge, "record_decision", return_value="recorded"
            ),
            patch.object(
                bot_mod.trade_ideas_bridge,
                "request_manual_fill",
                side_effect=RuntimeError("gateway down"),
            ),
        ):
            asyncio.run(bot_mod.on_callback(update, context))
        texts = [c.args[1] for c in context.bot.send_message.await_args_list]
        self.assertIn("Accepted idea #7", texts[0])
        self.assertIn("Couldn", texts[1])

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
