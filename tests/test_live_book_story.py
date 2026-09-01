"""The live book renders real capital, so it must carry its reasoning and mark.

Before this it was the only book on the dashboard shown as a bare table: no
rationale, no charts, and no mark — an open position never showed whether it
was up. Paper positions get spot and unrealized pnl from the paper engine;
live ledger rows carry neither, so both are derived in enrich_live_trades.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from dashboard import data


STORY = {
    "action": "deriv_buy",
    "rationale": "H4 supply tapped, M5 OB displaced.",
    "setup_tags": ["ob", "sfp"],
    "stop_loss": 2385.0,
    "take_profits": [2460.0],
    "risk_reward": 2.4,
    "chart_path": None,
    "marked_chart_paths": {"H4": "/charts/c1_H4.png"},
}

CHARTS = {
    "structure_chart_url": "/api/chart/c1?kind=structure&tf=H4",
    "execution_chart_url": "/api/chart/c1?kind=entry&tf=M5",
    "thumb_chart_url": "/api/chart/c1?kind=structure&tf=H4",
}


class EnrichLiveTradesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._patches = [
            patch.object(data, "_participation", return_value={}),
            patch.object(data, "trade_chart_urls", return_value=dict(CHARTS)),
            patch.object(
                data, "get_live_spots", return_value={"spots": {"ETH-USD": 2465.0}}
            ),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    @staticmethod
    def _row(**over):
        base = {
            "id": 1,
            "cycle_id": "c1",
            "source": "hq",
            "product_id": "ETH-USD",
            "side": "long",
            "qty": 0.4,
            "entry": 2411.5,
            "stop_loss": 2385.0,
            "take_profits_json": json.dumps([2460.0]),
            "fill_type": "auto",
            "opened_at": "2026-08-31T02:00:56Z",
        }
        base.update(over)
        return base

    def test_open_row_marks_to_spot(self) -> None:
        """The missing piece: an open live row had no mark and no unrealized."""
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            row = data.enrich_live_trades([self._row()])[0]
        self.assertEqual(row["spot"], 2465.0)
        self.assertAlmostEqual(row["pnl_usd"], (2465.0 - 2411.5) * 0.4, places=6)
        self.assertAlmostEqual(row["pnl_pct"], 21.4 / (2411.5 * 0.4) * 100, places=4)
        self.assertTrue(row["is_winner"])

    def test_short_position_profits_when_spot_falls(self) -> None:
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            row = data.enrich_live_trades(
                [self._row(side="short", entry=2500.0, qty=0.2)]
            )[0]
        self.assertAlmostEqual(row["pnl_usd"], (2500.0 - 2465.0) * 0.2, places=6)
        self.assertTrue(row["is_winner"])

    def test_closed_row_uses_recorded_exit_not_spot(self) -> None:
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            row = data.enrich_live_trades(
                [self._row(exit_price=2440.0, pnl_usd=-3.2, close_reason="stop")],
                closed=True,
            )[0]
        self.assertEqual(row["exit"], 2440.0)
        self.assertEqual(row["spot"], 2440.0)
        self.assertEqual(row["pnl_usd"], -3.2)
        self.assertFalse(row["is_winner"])

    def test_card_fields_come_from_the_cycle_story(self) -> None:
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            row = data.enrich_live_trades([self._row()])[0]
        self.assertEqual(row["rationale"], STORY["rationale"])
        self.assertEqual(row["risk_reward"], 2.4)
        self.assertEqual(row["risk_reward_kind"], "planned")
        self.assertEqual(row["take_profits"], [2460.0])
        self.assertEqual(row["product_label"], "ETH")
        self.assertEqual(row["thumb_chart_url"], CHARTS["thumb_chart_url"])

    def test_fill_type_leads_the_badges(self) -> None:
        """Auto vs manual is what an operator most wants at a glance."""
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            row = data.enrich_live_trades([self._row(fill_type="manual")])[0]
        self.assertEqual(row["setup_tags"], ["manual", "ob", "sfp"])

    def test_mill_clip_without_a_cycle_still_marks(self) -> None:
        """Mill clips carry no cycle_id, but must still show their P&L."""
        with patch.object(data, "_trade_story_from_cycle") as story:
            row = data.enrich_live_trades(
                [self._row(cycle_id=None, source="mill", qty=0.1, entry=2390.0)]
            )[0]
        story.assert_not_called()
        self.assertAlmostEqual(row["pnl_usd"], (2465.0 - 2390.0) * 0.1, places=6)
        self.assertEqual(row["rationale"], "")

    def test_unreadable_story_does_not_break_the_book(self) -> None:
        with patch.object(
            data, "_trade_story_from_cycle", side_effect=RuntimeError("no snapshot")
        ):
            row = data.enrich_live_trades([self._row()])[0]
        self.assertEqual(row["rationale"], "")
        self.assertAlmostEqual(row["pnl_usd"], (2465.0 - 2411.5) * 0.4, places=6)

    def test_missing_spot_does_not_invent_a_profit(self) -> None:
        with patch.object(
            data, "get_live_spots", return_value={"spots": {}}
        ), patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            row = data.enrich_live_trades([self._row()])[0]
        self.assertIsNone(row["spot"])
        self.assertEqual(row["pnl_usd"], 0.0)

    def test_only_the_remaining_size_is_marked_after_a_scale_out(self) -> None:
        """A banked tranche is realized — marking it again would double count."""
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            row = data.enrich_live_trades(
                [self._row(qty_open=0.3, realized_pnl_usd=2.85)]
            )[0]
        self.assertAlmostEqual(row["unrealized_pnl_usd"], (2465.0 - 2411.5) * 0.3)
        self.assertAlmostEqual(row["realized_pnl_usd"], 2.85)
        # Headline is banked plus still-riding.
        self.assertAlmostEqual(row["pnl_usd"], (2465.0 - 2411.5) * 0.3 + 2.85)
        self.assertTrue(row["scaled_out"])

    def test_untouched_row_is_not_flagged_as_scaled_out(self) -> None:
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            row = data.enrich_live_trades([self._row(qty_open=0.4)])[0]
        self.assertFalse(row["scaled_out"])
        self.assertEqual(row["realized_pnl_usd"], 0.0)

    def test_book_total_excludes_banked_profit(self) -> None:
        """Live performance already counts realized; the metric is mark-only."""
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            rows = data.enrich_live_trades(
                [self._row(qty_open=0.3, realized_pnl_usd=2.85)]
            )
        self.assertAlmostEqual(
            data.live_unrealized_usd(rows), (2465.0 - 2411.5) * 0.3
        )

    def test_original_ledger_row_is_not_mutated(self) -> None:
        original = self._row()
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            data.enrich_live_trades([original])
        self.assertNotIn("pnl_usd", original)

    def test_unrealized_total_sums_the_open_book(self) -> None:
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            rows = data.enrich_live_trades(
                [self._row(), self._row(id=2, qty=0.1, entry=2400.0)]
            )
        expected = (2465.0 - 2411.5) * 0.4 + (2465.0 - 2400.0) * 0.1
        self.assertAlmostEqual(data.live_unrealized_usd(rows), expected, places=6)

    def test_closed_card_reports_realized_r_not_the_planned_first_tp(self) -> None:
        """Eva #8: $17.85 on $10.60 of opening risk is 1.68R, not the 2.4 plan."""
        with patch.object(data, "_trade_story_from_cycle", return_value=STORY):
            row = data.enrich_live_trades(
                [
                    self._row(
                        exit_price=2456.12,
                        pnl_usd=17.85,
                        stop_loss=2440.0,
                        initial_stop_loss=2440.0,
                    )
                ],
                closed=True,
            )[0]
        self.assertEqual(row["risk_reward_kind"], "realized")
        self.assertAlmostEqual(row["risk_reward"], 1.68, places=2)

    def test_realized_r_is_pnl_over_opening_risk(self) -> None:
        self.assertEqual(
            data.realized_r_multiple(17.85, 0.4, 2411.5, 2385.0), 1.68
        )
        self.assertIsNone(data.realized_r_multiple(17.85, 0.4, 2411.5, None))

    def test_mill_clip_only_shows_the_armed_target(self) -> None:
        """One nano contract rests the whole clip on TP1 — TP2/TP3 are not live."""
        row = data.enrich_live_trades(
            [
                self._row(
                    cycle_id=None,
                    source="mill",
                    product_id="BTC-USD",
                    side="short",
                    qty=0.01,
                    entry=78305.0,
                    stop_loss=78629.22,
                    initial_stop_loss=78629.22,
                    take_profits_json=json.dumps(
                        [77254.43, 76704.51, 76154.59]
                    ),
                )
            ]
        )[0]
        self.assertEqual(len(row["tp_progress"]), 1)
        self.assertAlmostEqual(row["tp_progress"][0]["price"], 77254.43, places=2)
        self.assertEqual(row["risk_reward_kind"], "planned")
        self.assertAlmostEqual(row["risk_reward"], 3.24, places=2)


if __name__ == "__main__":
    unittest.main()
