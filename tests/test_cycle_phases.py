"""Cycle anatomy: phase clock, pivots, segments, projections."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from intelligence.cycle_phases import (
    CYCLE_LOWS,
    CYCLE_SPACING_DAYS,
    CYCLE_TOPS,
    build_segments,
    current_phase,
    cycle_position,
    next_pivot,
    projected_pivots,
)


def _bars(start: date, days: int, first: float, last: float) -> list[dict]:
    """Daily bars ramping linearly so segment math is checkable by hand."""
    step = (last - first) / max(days - 1, 1)
    return [
        {
            "ts": (start + timedelta(days=i)).isoformat() + "T00:00:00Z",
            "close": first + step * i,
        }
        for i in range(days)
    ]


class CyclePhaseTests(unittest.TestCase):
    def test_phase_clock_tracks_days_since_halving(self):
        phase, days = current_phase(date(2024, 5, 20))
        self.assertEqual(phase, "post_halving_accumulation")
        self.assertEqual(days, 30)

        phase, _ = current_phase(date(2025, 6, 1))
        self.assertEqual(phase, "bull_expansion")

        phase, _ = current_phase(date(2026, 8, 10))
        self.assertEqual(phase, "bear_drawdown")

    def test_projected_pivots_use_cycle_spacing(self):
        projected = projected_pivots(count=1)
        by_kind = {p.kind: p for p in projected}
        expected_top = date.fromisoformat(CYCLE_TOPS[-1]) + timedelta(
            days=CYCLE_SPACING_DAYS
        )
        expected_low = date.fromisoformat(CYCLE_LOWS[-1]) + timedelta(
            days=CYCLE_SPACING_DAYS
        )
        self.assertEqual(by_kind["top"].date, expected_top.isoformat())
        self.assertEqual(by_kind["low"].date, expected_low.isoformat())
        self.assertTrue(all(p.projected for p in projected))

    def test_segments_alternate_and_measure_moves(self):
        bars = _bars(date(2014, 1, 1), 365 * 10, 800.0, 60000.0)
        segments = build_segments(bars, as_of=date(2026, 8, 10))

        realized = [s for s in segments if not s.projected and not s.in_progress]
        kinds = [s.kind for s in realized]
        # low -> top is expansion, top -> low is drawdown, strictly alternating.
        self.assertEqual(kinds, ["drawdown", "expansion", "drawdown", "expansion", "drawdown"])

        live = [s for s in segments if s.in_progress]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].end_date, "2026-08-10")

        projected = [s for s in segments if s.projected]
        self.assertTrue(projected)
        self.assertTrue(all(s.change_pct is None for s in projected))

    def test_segment_change_pct_matches_prices(self):
        # Flat 100 then a doubling leg gives a clean +100% on the measured span.
        bars = [
            {"ts": f"{d.isoformat()}T00:00:00Z", "close": 100.0 if d < date(2022, 11, 7) else 200.0}
            for d in (date(2021, 11, 8) + timedelta(days=i) for i in range(400))
        ]
        segments = build_segments(bars, as_of=date(2022, 12, 1))
        drawdown = [
            s for s in segments
            if s.start_date == "2021-11-08" and s.end_date == "2022-11-07"
        ]
        self.assertEqual(len(drawdown), 1)
        self.assertEqual(drawdown[0].change_pct, 100.0)
        self.assertEqual(drawdown[0].kind, "drawdown")

    def test_segment_label_reads_like_the_chart(self):
        bars = _bars(date(2021, 1, 1), 900, 30000.0, 15000.0)
        seg = build_segments(bars, as_of=date(2023, 1, 1))[0]
        self.assertIn("bars", seg.label())

    def test_position_reports_drawdown_and_next_pivot(self):
        bars = _bars(date(2024, 1, 1), 900, 40000.0, 60000.0)
        bars.append({"ts": "2026-08-10T00:00:00Z", "close": 30000.0})
        position = cycle_position(bars, as_of=date(2026, 8, 10))

        self.assertEqual(position["phase"], "bear_drawdown")
        self.assertEqual(position["last_halving"], "2024-04-20")
        self.assertLess(position["drawdown_from_ath_pct"], 0)
        self.assertEqual(position["ath_price"], 60000.0)
        self.assertIsNotNone(position["next_projected_pivot"])
        self.assertGreater(position["days_to_next_pivot"], 0)

    def test_confirmed_top_splits_the_live_leg(self):
        # Rally off the 2022 low into a high, then a 50% breakdown from it.
        bars = _bars(date(2022, 11, 7), 800, 17000.0, 120000.0)
        bars += _bars(date(2024, 12, 16), 600, 120000.0, 60000.0)
        segments = build_segments(bars, as_of=date(2026, 8, 7))

        live = [s for s in segments if s.in_progress]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].kind, "drawdown")
        self.assertLess(live[0].change_pct, 0)

        # The leg before the live one is the expansion that made the high.
        expansion = [
            s for s in segments
            if s.start_date == "2022-11-07" and not s.in_progress and not s.projected
        ]
        self.assertEqual(len(expansion), 1)
        self.assertEqual(expansion[0].kind, "expansion")
        self.assertEqual(expansion[0].end_date, live[0].start_date)

    def test_shallow_pullback_does_not_confirm_a_top(self):
        bars = _bars(date(2022, 11, 7), 800, 17000.0, 120000.0)
        bars += _bars(date(2024, 12, 16), 200, 120000.0, 114000.0)  # -5%
        segments = build_segments(bars, as_of=date(2025, 7, 4))
        live = [s for s in segments if s.in_progress]
        self.assertEqual(live[0].kind, "expansion")

    def test_position_reports_confirmed_top(self):
        bars = _bars(date(2022, 11, 7), 800, 17000.0, 120000.0)
        bars += _bars(date(2024, 12, 16), 600, 120000.0, 60000.0)
        position = cycle_position(bars, as_of=date(2026, 8, 7))
        self.assertIsNotNone(position["confirmed_top"])
        self.assertTrue(position["confirmed_top"]["detected"])

    def test_next_pivot_is_always_in_the_future(self):
        pivot = next_pivot(date(2026, 8, 10))
        self.assertIsNotNone(pivot)
        self.assertGreater(date.fromisoformat(pivot.date), date(2026, 8, 10))


if __name__ == "__main__":
    unittest.main()
