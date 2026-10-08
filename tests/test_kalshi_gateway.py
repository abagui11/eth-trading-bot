"""Kalshi gateway quote/order helpers after the 2026 fixed-point migration."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import kalshi_gateway


class AskCentsTests(unittest.TestCase):
    def test_reads_ask_dollars(self) -> None:
        market = {"yes_ask_dollars": "0.6400", "no_ask_dollars": "0.3700"}
        self.assertEqual(kalshi_gateway.ask_cents(market, "yes"), 64)
        self.assertEqual(kalshi_gateway.ask_cents(market, "no"), 37)

    def test_falls_back_to_opposite_bid(self) -> None:
        # NO ask empty → 100 − YES bid
        market = {"yes_bid_dollars": "0.8200"}
        self.assertEqual(kalshi_gateway.ask_cents(market, "no"), 18)

    def test_still_accepts_legacy_integer_cents(self) -> None:
        market = {"yes_ask": 71}
        self.assertEqual(kalshi_gateway.ask_cents(market, "yes"), 71)

    def test_unquoted_returns_none(self) -> None:
        self.assertIsNone(kalshi_gateway.ask_cents({}, "yes"))
        self.assertIsNone(kalshi_gateway.ask_cents({"yes_ask_dollars": "0"}, "yes"))


class FillSummaryTests(unittest.TestCase):
    def test_summary_from_order_books_ioc_create_body(self) -> None:
        order = {
            "order_id": "oid-1",
            "status": "executed",
            "fill_count_fp": "38.00",
            "remaining_count_fp": "0.00",
            "no_price_dollars": "0.6400",
            "taker_fill_cost_dollars": "24.320000",
            "taker_fees_dollars": "0.612900",
        }
        summary = kalshi_gateway.summary_from_order(order, "no")
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary["contracts"], 38)
        self.assertEqual(summary["avg_cents"], 64.0)
        self.assertAlmostEqual(summary["cost_usd"], 24.93, places=2)
        self.assertTrue(kalshi_gateway.order_is_terminal(order))

    def test_aggregates_dollars_and_fp_counts(self) -> None:
        fills = [
            {
                "count_fp": "10.00",
                "yes_price_dollars": "0.7700",
                "fee_cost": "0.50",
            },
            {
                "count_fp": "5.00",
                "yes_price_dollars": "0.7800",
                "fee_cost": "0.25",
            },
        ]
        with patch.object(kalshi_gateway, "get_order_retry", side_effect=kalshi_gateway.KalshiError("404")), \
                patch.object(kalshi_gateway, "get_fills", return_value=fills):
            summary = kalshi_gateway.fill_summary("oid", "yes")
        self.assertEqual(summary["contracts"], 15)
        self.assertAlmostEqual(summary["avg_cents"], (10 * 77 + 5 * 78) / 15, places=2)
        self.assertAlmostEqual(summary["fee_usd"], 0.75, places=4)
        self.assertAlmostEqual(
            summary["cost_usd"],
            (10 * 0.77 + 5 * 0.78) + 0.75,
            places=2,
        )
        # Plural alias must stay wired — a typo here stranded live fills.
        self.assertIs(kalshi_gateway.fills_summary, kalshi_gateway.fill_summary)

    def test_unreadable_venue_raises_rather_than_claiming_zero(self) -> None:
        # A zero summary releases the caller's reserve; when neither the
        # fills endpoint nor the order answered, contracts may exist at the
        # venue and the only honest answer is "unknown" (keeps 'placing').
        with patch.object(kalshi_gateway, "get_fills",
                          side_effect=kalshi_gateway.KalshiError("down")), \
                patch.object(kalshi_gateway, "get_order_retry",
                             side_effect=kalshi_gateway.KalshiError("down")):
            with self.assertRaises(kalshi_gateway.KalshiError):
                kalshi_gateway.fill_summary("oid", "yes")

    def test_readably_empty_fills_still_report_zero(self) -> None:
        # Fills endpoint answered "nothing filled" — zero is a real answer
        # even when the order GET 404s.
        with patch.object(kalshi_gateway, "get_fills", return_value=[]), \
                patch.object(kalshi_gateway, "get_order_retry",
                             side_effect=kalshi_gateway.KalshiError("404")):
            summary = kalshi_gateway.fill_summary("oid", "yes")
        self.assertEqual(summary["contracts"], 0)

    def test_remaining_contracts_prefers_fp(self) -> None:
        self.assertEqual(
            kalshi_gateway.remaining_contracts({"remaining_count_fp": "0.00"}),
            0.0,
        )
        self.assertEqual(
            kalshi_gateway.remaining_contracts(
                {"remaining_count_fp": "3.50", "remaining_count": 99}
            ),
            3.5,
        )


class PlaceBodyTests(unittest.TestCase):
    def test_place_limit_buy_uses_v2_yes_book(self) -> None:
        captured: dict = {}

        def fake_request(method, endpoint, *, json_body=None, params=None):
            captured["method"] = method
            captured["endpoint"] = endpoint
            captured["body"] = json_body
            return {"order": {"order_id": "abc"}}

        with patch.object(kalshi_gateway, "_request", side_effect=fake_request), \
                patch.object(kalshi_gateway, "configured", return_value=True):
            order = kalshi_gateway.place_limit_buy("T", "yes", 12, 64)
        self.assertEqual(order["order_id"], "abc")
        self.assertEqual(captured["endpoint"], "/portfolio/events/orders")
        body = captured["body"]
        self.assertEqual(body["side"], "bid")
        self.assertEqual(body["count"], "12.00")
        # 64¢ ask + 2¢ take = 66¢ YES bid
        self.assertEqual(body["price"], "0.6600")
        self.assertEqual(body["time_in_force"], "immediate_or_cancel")

    def test_place_limit_buy_no_uses_ask_on_yes_book(self) -> None:
        captured: dict = {}

        def fake_request(method, endpoint, *, json_body=None, params=None):
            captured["body"] = json_body
            return {"order": {"order_id": "xyz"}}

        with patch.object(kalshi_gateway, "_request", side_effect=fake_request):
            kalshi_gateway.place_limit_buy("T", "no", 5, 40, take_cents=0)
        body = captured["body"]
        self.assertEqual(body["side"], "ask")
        # YES price for a 40¢ NO buy = 60¢
        self.assertEqual(body["price"], "0.6000")


if __name__ == "__main__":
    unittest.main()
