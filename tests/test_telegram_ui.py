"""Unit tests for Telegram beta keyboard and paper-account copy."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import bot_config
import config
from telegram_ui import (
    CB_OPEN,
    format_fund_result,
    format_metrics_message,
    format_open_account_result,
    main_keyboard,
)


class TelegramUiTests(unittest.TestCase):
    def test_main_keyboard_has_open_account_and_journal(self) -> None:
        with patch.object(config, "DASHBOARD_PUBLIC_URL", "https://dash.example"):
            keyboard = main_keyboard()

        buttons = [
            button
            for row in keyboard.inline_keyboard
            for button in row
        ]
        open_btn = next(button for button in buttons if button.text == "Open account")
        self.assertEqual(open_btn.callback_data, CB_OPEN)
        journal = next(button for button in buttons if button.text == "Agent journal")
        self.assertEqual(journal.url, "https://dash.example")
        my_book = next(button for button in buttons if button.text == "My book")
        self.assertEqual(my_book.callback_data, "ui:mybook")

    def test_format_open_account_result(self) -> None:
        success = format_open_account_result(
            {
                "ok": True,
                "amount_usd": 1000.0,
                "cash_usd": 1000.0,
            }
        )
        self.assertIn("Paper account opened", success)
        self.assertIn("$1,000", success)
        self.assertIn("not real funding", success)

        repeat = format_fund_result(
            {
                "ok": False,
                "reason": "already_funded",
                "amount_usd": 1000.0,
                "cash_usd": 1000.0,
            }
        )
        self.assertIn("Account already open", repeat)
        self.assertIn("$1,000", repeat)

    def test_format_metrics_message(self) -> None:
        message = format_metrics_message(
            {
                "ok": True,
                "amount_usd": 1000.0,
                "cash_usd": 900.0,
                "equity_usd": 1100.0,
                "pnl_usd": 100.0,
                "pnl_pct": 10.0,
                "open_count": 1,
            }
        )
        self.assertIn("My Metrics (personal demo)", message)
        self.assertIn("Starting capital: $1,000", message)
        self.assertIn("Equity: $1,100.00", message)
        self.assertIn("PnL: $+100.00 (+10.00%)", message)

    def test_format_metrics_message_before_account(self) -> None:
        with patch.object(bot_config, "PAPER_ACCOUNT_SIZES", (500.0, 1000.0, 2500.0)):
            message = format_metrics_message(
                {"ok": False, "reason": "not_funded"}
            )
        self.assertIn("not opened a paper account", message)
        self.assertIn("$500", message)


if __name__ == "__main__":
    unittest.main()
