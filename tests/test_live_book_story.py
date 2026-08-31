"""The live book renders real capital, so it must carry its reasoning.

Before this, `live_open` was the only book on the dashboard shown without the
rationale or charts behind it — the paper journal had both.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dashboard import data


class EnrichLiveTradesTests(unittest.TestCase):
    STORY = {
        "rationale": "H4 supply tapped, M5 OB displaced.",
        "setup_tags": ["ob", "sfp"],
        "stop_loss": 2385.0,
        "take_profits": [2460.0],
        "risk_reward": 2.4,
        "marked_chart_paths": {"H4": "/charts/c1_H4.png", "M5": "/charts/c1_M5.png"},
    }

    def test_hq_row_gains_story_and_chart_links(self) -> None:
        with patch.object(data, "_trade_story_from_cycle", return_value=self.STORY):
            rows = data.enrich_live_trades([{"id": 1, "cycle_id": "c1"}])
        row = rows[0]
        self.assertTrue(row["has_story"])
        self.assertEqual(row["story"]["risk_reward"], 2.4)
        self.assertEqual(
            [c["url"] for c in row["charts"]],
            [
                "/api/chart/c1?kind=marked&tf=H4",
                "/api/chart/c1?kind=marked&tf=M5",
            ],
        )

    def test_chart_links_only_for_timeframes_that_exist(self) -> None:
        """Links are driven by the snapshot so a row never offers a 404."""
        story = dict(self.STORY, marked_chart_paths={"M5": "/charts/c1_M5.png"})
        with patch.object(data, "_trade_story_from_cycle", return_value=story):
            rows = data.enrich_live_trades([{"id": 1, "cycle_id": "c1"}])
        self.assertEqual(
            [c["label"] for c in rows[0]["charts"]], ["M5 marked"]
        )

    def test_mill_clip_without_a_cycle_is_left_alone(self) -> None:
        with patch.object(data, "_trade_story_from_cycle") as story:
            rows = data.enrich_live_trades([{"id": 2, "cycle_id": None}])
        story.assert_not_called()
        self.assertFalse(rows[0]["has_story"])
        self.assertEqual(rows[0]["charts"], [])

    def test_unreadable_story_does_not_break_the_book(self) -> None:
        """A missing snapshot must not take the whole Trading Log down."""
        with patch.object(
            data, "_trade_story_from_cycle", side_effect=RuntimeError("no snapshot")
        ):
            rows = data.enrich_live_trades([{"id": 3, "cycle_id": "c9"}])
        self.assertFalse(rows[0]["has_story"])
        self.assertEqual(rows[0]["story"], {})

    def test_original_ledger_row_is_not_mutated(self) -> None:
        original = {"id": 4, "cycle_id": "c1"}
        with patch.object(data, "_trade_story_from_cycle", return_value=self.STORY):
            data.enrich_live_trades([original])
        self.assertNotIn("story", original)


if __name__ == "__main__":
    unittest.main()
