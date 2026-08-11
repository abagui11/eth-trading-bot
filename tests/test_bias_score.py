"""Unit tests for deterministic news bias scoring."""

from __future__ import annotations

import unittest

from macro.bias_score import best_bias, deterministic_bias


class BiasScoreTests(unittest.TestCase):
    def test_bullish_headline(self) -> None:
        out = deterministic_bias("Fed signals rate cut as ETF inflows surge", "bullish")
        self.assertEqual(out["side"], "bullish")
        self.assertGreaterEqual(out["pct"], 50)

    def test_bearish_headline(self) -> None:
        out = deterministic_bias("Exchange hack triggers mass liquidation near Hormuz", "bearish")
        self.assertEqual(out["side"], "bearish")
        self.assertGreaterEqual(out["pct"], 50)

    def test_best_bias_prefers_llm(self) -> None:
        event = {
            "title": "x",
            "bias_side_det": "bullish",
            "bias_pct_det": 40,
            "bias_side_llm": "bearish",
            "bias_pct_llm": 71,
            "bias_one_liner": "sell the news",
        }
        best = best_bias(event)
        self.assertEqual(best["side"], "bearish")
        self.assertEqual(best["pct"], 71)
        self.assertEqual(best["source"], "llm")


if __name__ == "__main__":
    unittest.main()
