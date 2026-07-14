"""Unit tests for dashboard display formatting helpers."""

from __future__ import annotations

import unittest

from dashboard.formatting import (
    format_trade_date,
    format_trade_time,
    tag_tooltip,
    trade_title,
)


class DashboardFormattingTests(unittest.TestCase):
    def test_trade_time_ampm_no_t(self) -> None:
        self.assertEqual(format_trade_time("2026-07-14T16:02:00Z"), "4:02 PM")
        self.assertEqual(format_trade_time("2026-07-14T14:41:00Z"), "2:41 PM")
        self.assertEqual(format_trade_time("2026-07-14T09:05:00Z"), "9:05 AM")
        self.assertNotIn("T", format_trade_time("2026-07-14T16:02:00Z"))

    def test_trade_date_and_title(self) -> None:
        self.assertEqual(format_trade_date("2026-07-14T16:02:00Z"), "Jul 14")
        self.assertEqual(
            trade_title("2026-07-14T16:02:00Z", "short"),
            "Jul 14 [short]",
        )

    def test_tag_tooltips(self) -> None:
        self.assertIn("24h", tag_tooltip("ranging"))
        self.assertIn("H4", tag_tooltip("h4_sfp_bearish"))
        self.assertIn("M5", tag_tooltip("m5_sfp_bullish"))
        self.assertIn("stop-loss", tag_tooltip("stop_loss").lower())


if __name__ == "__main__":
    unittest.main()
