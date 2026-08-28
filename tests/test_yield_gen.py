"""Yield Generation tab metrics — projected/earned carry and ETH-NAV P&L."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from dashboard.yield_gen import (
    align_eth_start,
    derive_yield_metrics,
    get_yield_payload,
    implied_eth_usd,
)


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
            net_eth_exposure=1.0,
            today=date(2026, 8, 27),
        )
        self.assertEqual(m["days_since_golive"], 90)
        self.assertEqual(m["yield_earned_usd"], 73.97)  # 300 * 90 / 365
        self.assertEqual(m["pnl_eth"], 0.0)
        self.assertEqual(m["pnl_ex_eth_usd"], 0.0)
        self.assertEqual(m["pnl_eth_price_usd"], 2000.0)
        self.assertEqual(m["realized_beta"], 1.0)
        self.assertEqual(m["eth_move_pct"], 1.0)

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
            net_eth_exposure=1.0,
            today=date(2026, 8, 27),
        )
        self.assertEqual(m["yield_earned_usd"], 90.0)
        self.assertAlmostEqual(m["pnl_eth"], 0.05, places=6)
        self.assertEqual(m["pnl_ex_eth_usd"], 100.0)
        self.assertEqual(m["pnl_eth_price_usd"], 0.0)
        self.assertIsNone(m["realized_beta"])

    def test_stable_book_eth_double_is_all_ex_eth_zero(self) -> None:
        m = derive_yield_metrics(
            nav_usd=2000,
            projected_usd=80,
            projected_apy=0.04,
            nav_eth_now=0.5,
            eth_price_now=4000,
            go_live_date="2026-05-29",
            nav_start_usd=2000,
            eth_price_start=2000,
            net_eth_exposure=0.0,
            today=date(2026, 8, 27),
        )
        self.assertEqual(m["pnl_ex_eth_usd"], 0.0)
        self.assertEqual(m["pnl_eth_price_usd"], 0.0)
        self.assertEqual(m["realized_beta"], 0.0)

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


class AlignEthStartTests(unittest.TestCase):
    def test_implies_from_collateral(self) -> None:
        self.assertAlmostEqual(implied_eth_usd(4041.64, 1.6205), 2494.07, places=1)

    def test_repairs_coingecko_print(self) -> None:
        px, repaired = align_eth_start(
            2442.01,
            start_collateral_usd=4041.64,
            collateral_eth=1.6205,
        )
        self.assertTrue(repaired)
        self.assertAlmostEqual(px, 2494.07, places=1)

    def test_keeps_aligned_print(self) -> None:
        px, repaired = align_eth_start(
            2494.07,
            start_collateral_usd=4041.64,
            collateral_eth=1.6205,
        )
        self.assertFalse(repaired)
        self.assertEqual(px, 2494.07)

    def test_fills_missing_start(self) -> None:
        px, repaired = align_eth_start(
            None,
            start_collateral_usd=4041.64,
            collateral_eth=1.6205,
        )
        self.assertTrue(repaired)
        self.assertIsNotNone(px)

    def test_leaves_stored_when_no_units(self) -> None:
        px, repaired = align_eth_start(
            2442.01,
            start_collateral_usd=4041.64,
            collateral_eth=None,
        )
        self.assertFalse(repaired)
        self.assertEqual(px, 2442.01)


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
                    "netEthExposure": "1.62",
                    "netBeta": "0.89",
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

    @patch("dashboard.yield_gen._fetch_json")
    @patch("dashboard.yield_gen.live_ledger")
    def test_repairs_coingecko_go_live_eth_to_aave_tape(self, ledger, fetch) -> None:
        fetch.side_effect = [
            {
                "enabled": True,
                "fetchedAt": "2026-08-28T00:00:00Z",
                "errors": [],
                "aave": [
                    {
                        "totalCollateralUsd": "3943.64",
                        "totalDebtUsd": "1600.55",
                        "healthFactor": "2.0",
                    }
                ],
                "pendle": [{"name": "PT-reUSD", "valuationUsd": "1594.65", "maturity": None}],
                "monitors": [],
                "topline": {
                    "projectedUsd": "298.67",
                    "projectedApy": "0.082",
                    "navUsd": "3937.74",
                    "navEthNow": "1.6157",
                    "ethPriceUsd": "2437.81",
                    "collateralEth": "1.6205",
                    "netEthExposure": "1.1355",
                    "netBeta": "0.70",
                },
            },
            {"plan": {"status": "blocked", "blockedReason": "credit", "actions": [], "warnings": []}},
        ]
        ledger.get_yield_nav_series.return_value = [
            {
                "snapshot_date": "2026-08-26",
                "nav_usd": 4005.53,
                "collateral_usd": 4041.64,
                "eth_price_usd": 2442.01,
            }
        ]
        with patch("dashboard.yield_gen.config") as cfg:
            cfg.YIELD_GEN_API_URL = "http://yield.local"
            cfg.YIELD_GEN_DASHBOARD_URL = "http://yield.local"
            payload = get_yield_payload()

        implied = 4041.64 / 1.6205
        ledger.set_yield_eth_price.assert_called_once()
        args, _kwargs = ledger.set_yield_eth_price.call_args
        self.assertEqual(args[0], "2026-08-26")
        self.assertAlmostEqual(args[1], implied, places=1)
        self.assertAlmostEqual(payload["eth_price_start_usd"], implied, places=1)
        self.assertLess(payload["eth_move_pct"], -0.02)


if __name__ == "__main__":
    unittest.main()
