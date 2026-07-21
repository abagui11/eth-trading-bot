"""Focused tests for Telegram trade-card callbacks (See more)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bot_config
import config
import paper
import telegram_ui
import user_books
from models import Suggestion


def _suggestion() -> Suggestion:
    return Suggestion(
        action="spot_sell",
        size=250.0,
        entry=65087.87,
        stop_loss=65723.40,
        take_profits=[64238.08],
        risk_reward=1.34,
        rationale=(
            "Canonical thesis with plenty of detail.\n\n"
            "Market context:\n• bearish M5 OB"
        ),
        product_id="BTC-USD",
    )


class SeeMoreCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_path = Path(self._tmpdir.name) / "test_ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", self._db_path),
            patch.object(config, "PAPER_PORTFOLIO_VALUE", 5000.0),
            patch.object(config, "ME_TOKEN_SECRET", "test-secret"),
            patch.object(config, "DASHBOARD_PUBLIC_URL", "https://dash.example"),
            patch.object(bot_config, "PAPER_ACCOUNT_SIZES", (500.0, 1000.0, 2500.0)),
            patch.object(bot_config, "APPROVAL_WINDOW_MIN", 15),
            patch.object(bot_config, "TRADE_DEPLOY_PCT", 0.25),
            patch.object(bot_config, "USER_MIN_DEPLOY_USD", 25.0),
            patch.object(bot_config, "HOUSE_CONTRIBUTION_TELEGRAM_ID", 0),
        ]
        for item in self._patches:
            item.start()
        paper.init_db()
        user_books.init_db()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self._tmpdir.cleanup()

    def test_see_more_sends_canonical_rationale(self) -> None:
        import bot as bot_mod

        charts_dir = Path(self._tmpdir.name)
        structure = charts_dir / "structure.png"
        entry = charts_dir / "entry.png"
        structure.write_bytes(b"png")
        entry.write_bytes(b"png")

        offer = user_books.create_trade_offer(
            cycle_id="20260721T160000Z_BTC",
            suggestion=_suggestion(),
            chart_paths=[
                str(charts_dir / "decision.png"),
                str(structure),
                str(entry),
            ],
            display_summary="Friendly blurb.",
        )
        assert offer is not None

        query = MagicMock()
        query.data = f"{telegram_ui.CB_TRADE_MORE_PREFIX}{offer['offer_id']}"
        query.from_user = MagicMock(id=42, username="alice")
        query.message = MagicMock(chat_id=42)
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        update.effective_user = query.from_user

        context = MagicMock()
        context.bot = MagicMock()
        context.bot.send_message = AsyncMock()
        context.bot.send_photo = AsyncMock()

        with (
            patch.object(bot_mod.access, "register_user"),
            patch.object(bot_mod.access, "is_allowed", return_value=True),
            patch.object(
                bot_mod.research,
                "get_spot_prices",
                return_value={"ETH-USD": 1900.0, "BTC-USD": 65000.0},
            ),
            patch.object(
                bot_mod.paper, "format_pnl_footer", return_value="Paper PnL: n/a"
            ),
        ):
            asyncio.run(bot_mod.on_callback(update, context))

        self.assertGreaterEqual(context.bot.send_photo.await_count, 1)
        self.assertGreaterEqual(context.bot.send_message.await_count, 1)
        text = context.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Canonical thesis", text)
        self.assertIn("Why this trade", text)
        self.assertIn("Market context", text)
        self.assertIn("Entry: 65,087.87", text)
        # Original Accept window / decision is untouched — See more only reads.
        decision = user_books.get_offer(offer["offer_id"])
        self.assertEqual(decision["display_summary"], "Friendly blurb.")

    def test_see_more_missing_offer(self) -> None:
        import bot as bot_mod

        query = MagicMock()
        query.data = f"{telegram_ui.CB_TRADE_MORE_PREFIX}missing_offer"
        query.from_user = MagicMock(id=7, username="bob")
        query.message = MagicMock(chat_id=7)
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        update.effective_user = query.from_user
        context = MagicMock()
        context.bot = MagicMock()
        context.bot.send_message = AsyncMock()

        with (
            patch.object(bot_mod.access, "register_user"),
            patch.object(bot_mod.access, "is_allowed", return_value=True),
        ):
            asyncio.run(bot_mod.on_callback(update, context))

        text = context.bot.send_message.call_args.args[1]
        self.assertIn("Could not find", text)


if __name__ == "__main__":
    unittest.main()
