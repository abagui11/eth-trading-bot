"""Tests for dashboard status headline formatting."""

import unittest

from dashboard.status import format_agent_status


class DashboardStatusTests(unittest.TestCase):
    def test_awaiting_retest_phase(self) -> None:
        snapshot = {
            "cycle_id": "20260702T120000Z",
            "ts": "2026-07-02T12:00:00Z",
            "snapshot": {
                "setup_state": {
                    "phase": "awaiting_bearish_retest",
                    "retest_low": 1568.0,
                    "retest_high": 1584.0,
                },
                "zone_snapshot": {
                    "bearish_retest_low": 1568.0,
                    "bearish_retest_high": 1584.0,
                },
                "alerts": [],
            },
            "suggestion": {"action": "no_trade"},
        }
        status = format_agent_status(snapshot)
        self.assertIn("bearish", status["headline"].lower())
        self.assertEqual(status["phase"], "awaiting_bearish_retest")
        self.assertTrue(status["watching"])

    def test_open_position_headline(self) -> None:
        positions = [
            {
                "side": "short",
                "avg_entry": 1570.0,
                "stop_loss": 1620.0,
                "take_profits": [1500.0],
            }
        ]
        status = format_agent_status(None, open_positions=positions)
        self.assertIn("short", status["headline"].lower())
        self.assertIn("1,570", status["headline"])


if __name__ == "__main__":
    unittest.main()
