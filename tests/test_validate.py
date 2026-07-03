"""Tests for trade risk validation."""

from __future__ import annotations

import unittest

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

    def test_rejects_micro_stop(self) -> None:
        s = self._short(entry=1576.0, stop_loss=1577.0, take_profits=[1574.0])
        with self.assertRaisesRegex(ValueError, "stop distance"):
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

    def test_rejects_insufficient_risk_capacity(self) -> None:
        # 0.25% stop is guide minimum but needs 4x notional on $1k unleveraged paper.
        s = self._short(
            entry=1600.0,
            stop_loss=1604.0,
            take_profits=[1580.0],
        )
        with self.assertRaisesRegex(ValueError, "stop too tight for 1% risk"):
            validate.validate_trade_risk(s, portfolio_value=1000.0)

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
            "structure_chart": "H12",
            "entry_chart": "H1",
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
