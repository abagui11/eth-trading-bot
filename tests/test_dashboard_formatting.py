"""Unit tests for dashboard display formatting helpers."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from dashboard.formatting import (
    format_news_age,
    format_news_when,
    format_trade_date,
    format_trade_time,
    news_source_label,
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

    def test_news_when_handles_iso_and_rss_stamps(self) -> None:
        # Ingest timestamps are ISO; RSS pubDate is RFC 2822.
        self.assertEqual(
            format_news_when("2026-08-10T10:45:43Z"), "Aug 10 · 10:45 UTC"
        )
        self.assertEqual(
            format_news_when("Mon, 10 Aug 2026 10:45:43 +0000"), "Aug 10 · 10:45 UTC"
        )
        self.assertEqual(format_news_when(None), "")
        self.assertEqual(format_news_when("not a date"), "")

    def test_news_age_buckets(self) -> None:
        now = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(format_news_age("2026-08-10T17:59:40Z", now=now), "just now")
        self.assertEqual(format_news_age("2026-08-10T17:38:00Z", now=now), "22m ago")
        self.assertEqual(format_news_age("2026-08-10T12:00:00Z", now=now), "6h ago")
        self.assertEqual(format_news_age("2026-08-07T18:00:00Z", now=now), "3d ago")
        self.assertEqual(format_news_age(None, now=now), "")

    def test_news_source_prefers_url_host(self) -> None:
        self.assertEqual(
            news_source_label(
                {"url": "https://www.cnbc.com/2026/08/10/x.html", "source": "US Top News"}
            ),
            "cnbc.com",
        )
        # No URL: fall back to the feed title, trimmed at its colon.
        self.assertEqual(
            news_source_label({"source": "CoinDesk: Bitcoin, Ethereum, Crypto News"}),
            "CoinDesk",
        )
        self.assertEqual(news_source_label({}), "")

    def test_tag_tooltips(self) -> None:
        self.assertIn("24h", tag_tooltip("ranging"))
        self.assertIn("Ranging", tag_tooltip("ranging"))
        self.assertIn("SFP", tag_tooltip("h4_sfp_bullish"))
        self.assertIn("H4", tag_tooltip("h4_sfp_bearish"))
        self.assertIn("M5", tag_tooltip("m5_sfp_bullish"))
        self.assertIn("0.25", tag_tooltip("m5_ob_bearish_in_fib"))
        self.assertIn("stop-loss", tag_tooltip("stop_loss").lower())
        self.assertIn("Macro", tag_tooltip("macro_gate_long"))
        # Unknown tags still get a readable tip (never blank / "?").
        tip = tag_tooltip("some_future_tag")
        self.assertTrue(tip)
        self.assertNotEqual(tip.strip(), "?")
        self.assertIn("some future tag", tip)


if __name__ == "__main__":
    unittest.main()
