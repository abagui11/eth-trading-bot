"""Yield Generation tab metrics — projected/earned carry and ETH-NAV P&L."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from dashboard.yield_gen import derive_yield_metrics, get_yield_payload


class DeriveYieldMetricsTests(unittest.TestCase):
    def test_strips_pure_eth_price_move(self) -> None:
        m = derive_yield_metrics(
            nav_usd=4000,
            projected_usd=300,
            projected_apy=0.08,
            nav_eth_now=1.0,
            eth_price_now=4000,
            go_live_date="2026-05-29",
            nav_start_usd=2000,
            eth_price_start=2000,
            today=date(2026, 8, 27),
        )
        self.assertEqual(m["days_since_golive"], 90)
        self.assertEqual(m["yield_earned_usd"], 73.97)  # 300 * 90 / 365
        self.assertEqual(m["pnl_eth"], 0.0)

    def test_keeps_carry_when_eth_unchanged(self) -> None:
        m = derive_yield_metrics(
            nav_usd=2100,
            projected_usd=365,
            projected_apy=0.1,
            nav_eth_now=None,
            eth_price_now=2000,
            go_live_date="2026-05-29",
            nav_start_usd=2000,
            eth_price_start=2000,
            today=date(2026, 8, 27),
        )
        self.assertEqual(m["yield_earned_usd"], 90.0)
        self.assertAlmostEqual(m["pnl_eth"], 0.05, places=6)

    def test_eth_pnl_null_without_start_price(self) -> None:
        m = derive_yield_metrics(
            nav_usd=2100,
            projected_usd=365,
            projected_apy=0.1,
            nav_eth_now=1.05,
            eth_price_now=2000,
            go_live_date="2026-05-29",
            nav_start_usd=2000,
            eth_price_start=None,
            today=date(2026, 8, 27),
        )
        self.assertIsNone(m["pnl_eth"])
        self.assertEqual(m["nav_eth"], 1.05)


class YieldPayloadTests(unittest.TestCase):
    @patch("dashboard.yield_gen._fetch_json")
    @patch("dashboard.yield_gen.live_ledger")
    def test_payload_includes_carry_fields(self, ledger, fetch) -> None:
        fetch.side_effect = [
            {
                "enabled": True,
                "fetchedAt": "2026-08-27T00:00:00Z",
                "errors": [],
                "aave": [
                    {
                        "totalCollateralUsd": "4000",
                        "totalDebtUsd": "1560",
                        "healthFactor": "2.0",
                    }
                ],
                "pendle": [{"name": "PT-reUSD", "valuationUsd": "1200", "maturity": None}],
                "monitors": [],
                "topline": {
                    "projectedUsd": "298.67",
                    "projectedApy": "0.082",
                    "navUsd": "3640",
                    "navEthNow": "1.82",
                    "ethPriceUsd": "2000",
                },
            },
            {"plan": {"status": "blocked", "blockedReason": "credit", "actions": [], "warnings": []}},
        ]
        ledger.get_yield_nav_series.return_value = [
            {
                "snapshot_date": "2026-05-29",
                "nav_usd": 3500,
                "eth_price_usd": 1750,
            },
            {
                "snapshot_date": "2026-08-26",
                "nav_usd": 3600,
                "eth_price_usd": 1980,
            },
        ]
        with patch("dashboard.yield_gen.config") as cfg:
            cfg.YIELD_GEN_API_URL = "http://yield.local"
            cfg.YIELD_GEN_DASHBOARD_URL = "http://yield.local"
            payload = get_yield_payload()

        self.assertTrue(payload["available"])
        self.assertEqual(payload["nav_usd"], 3640.0)
        self.assertEqual(payload["yield_projected_usd"], 298.67)
        self.assertEqual(payload["go_live_date"], "2026-05-29")
        self.assertEqual(payload["eth_price_start_usd"], 1750.0)
        self.assertIsNotNone(payload["pnl_eth"])
        self.assertEqual(payload["plan"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
