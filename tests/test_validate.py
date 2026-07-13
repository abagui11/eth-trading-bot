"""Tests for trade risk validation."""

from __future__ import annotations

import unittest

import bot_config
import validate
from models import Suggestion


class ValidateTradeRiskTests(unittest.TestCase):
    def _short(self, **kwargs) -> Suggestion:
        defaults = dict(
            action="deriv_sell",
            size=0.64,
            entry=1576.0,
            stop_loss=1592.0,
            take_profits=[1545.0, 1515.0, 1490.0],
            risk_reward=99.0,
            rationale="test",
        )
        defaults.update(kwargs)
        return Suggestion(**defaults)

    def test_accepts_well_sized_short(self) -> None:
        s = self._short()
        validate.validate_trade_risk(s, portfolio_value=1000.0)
        self.assertAlmostEqual(s.risk_reward, 1.9375, places=3)
        self.assertGreater(s.size, 0)

    def test_validate_sizes_from_live_equity(self) -> None:
        s = self._short()
        validate.validate_trade_risk(s, portfolio_value=4500.0, cash=4500.0)
        expected = 4500.0 * bot_config.TRADE_DEPLOY_PCT / float(s.entry)
        self.assertAlmostEqual(s.size, round(expected, 4), places=4)

    def test_overwrites_inflated_llm_size(self) -> None:
        s = self._short(size=0.99)
        validate.validate_trade_risk(s, portfolio_value=5000.0, cash=5000.0)
        expected = validate.compute_eth_qty(
            float(s.entry), float(s.stop_loss), cash=5000.0, equity_usd=5000.0
        )
        self.assertAlmostEqual(s.size, round(expected, 4), places=4)
        self.assertNotEqual(s.size, 0.99)

    def test_compute_eth_qty_deploys_fixed_fraction(self) -> None:
        qty = validate.compute_eth_qty(1700.0, 1717.0, cash=5000.0, equity_usd=5000.0)
        expected = 5000.0 * bot_config.TRADE_DEPLOY_PCT / 1700.0
        self.assertAlmostEqual(qty, expected, places=4)

    def test_compute_eth_qty_caps_by_available_cash(self) -> None:
        qty = validate.compute_eth_qty(1700.0, 1717.0, cash=1000.0, equity_usd=5000.0)
        self.assertAlmostEqual(qty, 1000.0 / 1700.0, places=4)

    def test_compute_eth_qty_ignores_stop_distance(self) -> None:
        # Same portfolio/entry → same size regardless of stop width (sizing is deployment-based).
        tight = validate.compute_eth_qty(1700.0, 1704.0, cash=5000.0, portfolio_value=5000.0)
        wide = validate.compute_eth_qty(1700.0, 1200.0, cash=5000.0, portfolio_value=5000.0)
        self.assertAlmostEqual(tight, wide, places=6)

    def test_compute_eth_qty_caps_at_max(self) -> None:
        qty = validate.compute_eth_qty(
            1700.0, 1717.0, cash=1_000_000.0, portfolio_value=1_000_000.0
        )
        self.assertAlmostEqual(qty, bot_config.MAX_ETH_QTY, places=4)

    def test_compute_eth_qty_raises_to_min_when_affordable(self) -> None:
        # 25% of a small portfolio is below MIN_ETH_QTY; bumps up when cash allows.
        qty = validate.compute_eth_qty(1700.0, 1200.0, cash=5000.0, portfolio_value=1000.0)
        self.assertAlmostEqual(qty, bot_config.MIN_ETH_QTY, places=4)

    def test_rejects_micro_stop(self) -> None:
        s = self._short(entry=1576.0, stop_loss=1577.0, take_profits=[1574.0])
        with self.assertRaisesRegex(ValueError, "stop distance"):
            validate.validate_trade_risk(s, portfolio_value=1000.0)

    def test_accepts_one_to_one_risk_reward(self) -> None:
        s = self._short(
            entry=1600.0,
            stop_loss=1616.0,
            take_profits=[1584.0],
            risk_reward=2.0,
        )
        validate.validate_trade_risk(s, portfolio_value=1000.0)
        self.assertAlmostEqual(s.risk_reward, 1.0, places=3)

    def test_rejects_below_one_to_one_risk_reward(self) -> None:
        s = self._short(
            entry=1600.0,
            stop_loss=1616.0,
            take_profits=[1585.0],
            risk_reward=2.0,
        )
        with self.assertRaisesRegex(ValueError, "recomputed R/R"):
            validate.validate_trade_risk(s, portfolio_value=1000.0)

    def test_rejects_inflated_llm_risk_reward(self) -> None:
        s = self._short(
            entry=1576.0,
            stop_loss=1592.0,
            take_profits=[1570.0],
            risk_reward=5.0,
        )
        with self.assertRaisesRegex(ValueError, "recomputed R/R"):
            validate.validate_trade_risk(s, portfolio_value=1000.0)

    def test_rejects_stop_on_wrong_side(self) -> None:
        s = self._short(entry=1576.0, stop_loss=1570.0, take_profits=[1545.0])
        with self.assertRaisesRegex(ValueError, "must be above entry"):
            validate.validate_trade_risk(s, portfolio_value=1000.0)

    def test_accepts_tight_stop_with_fixed_sizing(self) -> None:
        # Guide-minimum 0.25% stop is fine now: sizing is deployment-based, not risk-based,
        # so a tight stop no longer needs unaffordable notional to hit a risk target.
        s = self._short(
            entry=1600.0,
            stop_loss=1604.0,
            take_profits=[1580.0],
        )
        validate.validate_trade_risk(s, portfolio_value=1000.0)
        self.assertGreater(s.size, 0)

    def test_long_recomputes_risk_reward(self) -> None:
        s = Suggestion(
            action="spot_buy",
            size=0.32,
            entry=1572.0,
            stop_loss=1543.0,
            take_profits=[1620.0, 1643.91],
            risk_reward=2.27,
            rationale="test",
        )
        validate.validate_trade_risk(s, portfolio_value=1000.0)
        self.assertAlmostEqual(s.risk_reward, 1.6552, places=3)

    def test_no_trade_skips_validation(self) -> None:
        s = Suggestion.no_trade("flat")
        validate.validate_trade_risk(s, portfolio_value=1000.0)
        self.assertIsNone(s.risk_reward)


class AnalyzeIntegrationTests(unittest.TestCase):
    def test_validate_rejects_tight_trade_in_pipeline(self) -> None:
        from analyze import _validate

        data = {
            "action": "deriv_sell",
            "size": 0.5,
            "entry": 1576.0,
            "stop_loss": 1577.0,
            "take_profits": [1574.0],
            "risk_reward": 2.0,
            "rationale": "tight",
            "structure_chart": "H4",
            "entry_chart": "M5",
            "order_block": {
                "low": 1568.0,
                "high": 1584.0,
                "start_ts": "2026-06-20T12:00:00Z",
                "end_ts": "2026-06-20T12:00:00Z",
            },
        }
        with self.assertRaises(ValueError):
            _validate(data)


if __name__ == "__main__":
    unittest.main()
