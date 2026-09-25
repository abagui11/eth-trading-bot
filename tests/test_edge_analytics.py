"""Investor Analytics tab: the password gate and the stats it serves."""

from __future__ import annotations

import unittest
from unittest import mock

import random

from dashboard import edge_analytics


class TestStats(unittest.TestCase):
    def _stats(self, rows, *, base_usd: float = 100.0):
        return edge_analytics._stats(rows, base_usd=base_usd, rng=random.Random(7))

    def test_empty_book_is_all_nulls(self) -> None:
        s = self._stats([])
        self.assertEqual(s["n"], 0)
        self.assertIsNone(s["p_edge"])
        self.assertEqual(s["equity"], [])

    def test_totals_and_equity_are_percent_of_base(self) -> None:
        rows = [
            ("2026-09-02T10:00:00Z", 5.0),
            ("2026-09-01T10:00:00Z", -2.0),
            ("2026-09-03T10:00:00Z", 1.0),
        ]
        s = self._stats(rows, base_usd=100.0)
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["days"], 3)
        self.assertAlmostEqual(s["total"], 4.0)
        self.assertEqual([p[1] for p in s["equity"]], [-2.0, 3.0, 4.0])

    def test_all_positive_days_bootstrap_to_certainty(self) -> None:
        rows = [(f"2026-09-{d:02d}T10:00:00Z", 1.0) for d in range(1, 11)]
        s = self._stats(rows)
        self.assertEqual(s["p_edge"], 1.0)
        self.assertEqual(s["win_pct"], 100)

    def test_same_day_trades_are_one_cluster(self) -> None:
        # 30 trades on one day: too few day-clusters for P(edge) or Sharpe.
        rows = [("2026-09-01T10:00:00Z", 1.0)] * 30
        s = self._stats(rows)
        self.assertEqual(s["days"], 1)
        self.assertIsNone(s["p_edge"])
        self.assertIsNone(s["sharpe"])

    def test_sharpe_needs_five_days(self) -> None:
        rows = [(f"2026-09-{d:02d}T10:00:00Z", float(d % 3 - 1)) for d in range(1, 5)]
        self.assertIsNone(self._stats(rows)["sharpe"])
        rows = [(f"2026-09-{d:02d}T10:00:00Z", float(d % 3 - 1)) for d in range(1, 6)]
        self.assertIsNotNone(self._stats(rows)["sharpe"])

    def test_kalshi_fee_matches_the_recorded_fill(self) -> None:
        # 8 contracts at 67c cost $0.1239 on the exchange (2026-09-22 fill)
        fee = edge_analytics._kalshi_fee_per_ct(67.0) * 8
        self.assertAlmostEqual(fee, 0.1239, places=3)

    def test_yield_eth_growth_from_levels(self) -> None:
        # Flat ETH NAV → near-zero growth (the "straight line" case).
        levels = [
            ("2026-09-01T23:59:00Z", 1.60),
            ("2026-09-02T23:59:00Z", 1.59),
            ("2026-09-03T23:59:00Z", 1.605),
        ]
        equity, total, base = edge_analytics._pct_equity_from_levels(levels)
        self.assertAlmostEqual(base, 1.60)
        self.assertEqual(equity[0][1], 0.0)
        self.assertAlmostEqual(total, 100.0 * (1.605 / 1.60 - 1.0), places=2)

    def test_mill_paper_book_keeps_closed_pct_only(self) -> None:
        trades = [
            {"status": "hit_tp", "closed_at": "2026-09-13T10:00:00Z", "pnl_pct": 1.2},
            {"status": "hit_sl", "closed_at": "2026-09-14T10:00:00Z", "pnl_pct": -0.8},
            {"status": "open", "closed_at": None, "pnl_pct": None},
            {"status": "hit_tp", "closed_at": None, "pnl_pct": 0.5},  # no close ts
        ]
        with mock.patch(
            "trade_ideas_bridge.mill_paper_trades_since", return_value=trades
        ):
            rows = edge_analytics._mill_paper_book()
        self.assertEqual(rows, [
            ("2026-09-13T10:00:00Z", 1.2),
            ("2026-09-14T10:00:00Z", -0.8),
        ])


class TestPasswordGate(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from dashboard.app import create_app

        self.client = TestClient(create_app())

    def test_wrong_password_is_401(self) -> None:
        r = self.client.post("/api/analytics/edge", json={"password": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_right_password_serves_the_payload(self) -> None:
        with mock.patch(
            "dashboard.edge_analytics.build_edge_payload",
            return_value={"books": [], "scaling": None,
                          "generated_at": "t", "cache_ttl_sec": 600,
                          "method": ""},
        ):
            r = self.client.post(
                "/api/analytics/edge", json={"password": "evatradesforyou"}
            )
        self.assertEqual(r.status_code, 200)
        self.assertIn("books", r.json())

    def test_missing_password_is_422_not_500(self) -> None:
        r = self.client.post("/api/analytics/edge", json={})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
