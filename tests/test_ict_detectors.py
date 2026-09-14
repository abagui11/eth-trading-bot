"""Tests for the FVG and BOS/CHoCH detectors.

The load-bearing tests here are the causality ones. Both detectors exist
because the `smartmoneyconcepts` equivalents read future bars and retroactively
rewrite past signals; if our ports drift into the same behaviour the day
variant's backtest becomes optimistic in a way live trading will not honour.
"""

from __future__ import annotations

import pandas as pd
import pytest

from patterns.fvg import detect_fvgs, nearest_open_fvg
from patterns.structure_shift import (
    current_trend,
    detect_structure_breaks,
    latest_structure_break,
)


def _df(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """bars = [(open, high, low, close), ...] on a 1-minute index."""
    idx = pd.date_range("2026-09-14", periods=len(bars), freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [b[0] for b in bars],
            "high": [b[1] for b in bars],
            "low": [b[2] for b in bars],
            "close": [b[3] for b in bars],
            "volume": [1.0] * len(bars),
        },
        index=idx,
    )


class TestFVG:
    def test_bullish_gap_detected_with_correct_bounds(self):
        # bar0 high 100, bar2 low 104 -> gap (100, 104), bar2 is bullish
        df = _df([(98, 100, 97, 99), (100, 108, 100, 107), (104, 110, 104, 109)])
        gaps = detect_fvgs(df)
        assert len(gaps) == 1
        g = gaps[0]
        assert g.direction == "bullish"
        assert (g.bottom, g.top) == (100.0, 104.0)
        assert g.midpoint == 102.0
        assert g.idx == 2  # attributed to the completing bar, not the middle

    def test_bearish_gap_detected_with_correct_bounds(self):
        df = _df([(102, 103, 100, 101), (99, 100, 92, 93), (96, 96, 90, 91)])
        gaps = detect_fvgs(df)
        assert len(gaps) == 1
        g = gaps[0]
        assert g.direction == "bearish"
        assert (g.bottom, g.top) == (96.0, 100.0)

    def test_no_gap_when_ranges_overlap(self):
        df = _df([(98, 105, 97, 104), (104, 108, 103, 107), (107, 110, 102, 109)])
        assert detect_fvgs(df) == []

    def test_bullish_displacement_with_bearish_third_bar_is_not_a_gap(self):
        """Direction must agree with the candle that completes the gap."""
        df = _df([(98, 100, 97, 99), (100, 108, 100, 107), (109, 110, 104, 105)])
        assert detect_fvgs(df) == []

    def test_tiny_gap_below_threshold_is_noise(self):
        df = _df([(98, 100.0, 97, 99), (100, 108, 100, 107),
                  (100.01, 110, 100.01, 109)])
        assert detect_fvgs(df) == []

    def test_mitigation_recorded_when_price_returns(self):
        df = _df([
            (98, 100, 97, 99), (100, 108, 100, 107), (104, 110, 104, 109),
            (109, 111, 108, 110),
            (108, 109, 101, 102),   # trades back into (100, 104)
        ])
        g = detect_fvgs(df)[0]
        assert g.mitigated_idx == 4
        assert g.is_open is False
        assert detect_fvgs(df, open_only=True) == []

    def test_unmitigated_gap_stays_open(self):
        df = _df([
            (98, 100, 97, 99), (100, 108, 100, 107), (104, 110, 104, 109),
            (109, 112, 106, 111),   # stays above the gap top of 104
        ])
        g = detect_fvgs(df)[0]
        assert g.is_open is True
        assert len(detect_fvgs(df, open_only=True)) == 1

    def test_nearest_open_fvg_picks_closest_by_midpoint(self):
        df = _df([
            (98, 100, 97, 99), (100, 108, 100, 107), (104, 110, 104, 109),
            (109, 112, 109, 111), (115, 130, 115, 129), (120, 135, 120, 134),
        ])
        gaps = detect_fvgs(df, open_only=True)
        assert len(gaps) >= 2
        near = nearest_open_fvg(df, price=103.0, direction="bullish")
        assert near is not None and near.midpoint == 102.0

    def test_contains_is_inclusive_of_bounds(self):
        df = _df([(98, 100, 97, 99), (100, 108, 100, 107), (104, 110, 104, 109)])
        g = detect_fvgs(df)[0]
        assert g.contains(100.0) and g.contains(104.0) and g.contains(102.0)
        assert not g.contains(99.9)

    def test_causality_appending_bars_never_changes_existing_gap_geometry(self):
        """A gap, once formed, must not be redefined by later bars.

        This is the `smartmoneyconcepts` failure mode restated as a property.
        """
        base = [(98, 100, 97, 99), (100, 108, 100, 107), (104, 110, 104, 109)]
        first = detect_fvgs(_df(base))[0]
        extended = base + [(109, 140, 108, 139), (139, 150, 138, 149)]
        after = detect_fvgs(_df(extended))[0]
        assert (after.idx, after.direction, after.top, after.bottom) == (
            first.idx, first.direction, first.top, first.bottom
        )


def _trend_bars() -> list[tuple[float, float, float, float]]:
    """Swing high at 110, then a swing low at 98, then a close through 110.

    `find_pivots` needs a *strict* local extreme, so neighbouring bars must not
    tie the pivot's high/low or no pivot confirms at all.
    """
    return [
        (100, 101, 99, 100),
        (101, 103, 100, 102),
        (102, 110, 102, 109),   # idx 2: swing high 110, confirmed at idx 4
        (109, 109, 106, 107),
        (107, 108, 105, 106),
        (106, 107, 98, 99),     # idx 5: swing low 98, confirmed at idx 7
        (99, 102, 99, 101),
        (101, 105, 100, 104),
        (104, 112, 104, 111),   # idx 8: closes above 110 -> bullish break
        (111, 113, 110, 112),
    ]


class TestStructureBreaks:
    def test_bullish_break_of_the_prior_swing_high_is_detected(self):
        breaks = detect_structure_breaks(_df(_trend_bars()), left=2, right=2)
        bullish = [b for b in breaks if b.direction == "bullish"]
        assert bullish, "expected a bullish break of the 110 swing high"
        assert bullish[0].level == 110.0

    def test_break_is_dated_to_the_bar_that_broke_it_not_the_pivot(self):
        breaks = detect_structure_breaks(_df(_trend_bars()), left=2, right=2)
        b = [x for x in breaks if x.direction == "bullish"][0]
        assert b.idx > b.pivot_idx
        assert b.pivot_idx == 2

    def test_choch_fires_when_trend_reverses(self):
        bars = _trend_bars() + [
            (112, 113, 111, 112),
            (112, 112, 104, 105),
            (105, 106, 103, 104),
            (104, 105, 95, 96),    # closes below the 98 swing low
            (96, 97, 94, 95),
        ]
        breaks = detect_structure_breaks(_df(bars), left=2, right=2)
        kinds = [(b.direction, b.kind) for b in breaks]
        assert ("bearish", "choch") in kinds, kinds

    def test_a_level_fires_at_most_once(self):
        bars = _trend_bars() + [
            (112, 113, 111, 112), (112, 114, 111, 113), (113, 115, 112, 114),
        ]
        breaks = detect_structure_breaks(_df(bars), left=2, right=2)
        levels = [b.level for b in breaks if b.direction == "bullish"]
        assert len(levels) == len(set(levels))

    def test_close_break_is_stricter_than_wick_break(self):
        # Wicks to 111 but closes at 109, below the 110 swing high.
        bars = _trend_bars()[:8] + [(104, 111, 104, 109), (109, 110, 108, 109)]
        df = _df(bars)
        assert not [b for b in detect_structure_breaks(df, left=2, right=2,
                                                       use_close=True)
                    if b.direction == "bullish"]
        assert [b for b in detect_structure_breaks(df, left=2, right=2,
                                                   use_close=False)
                if b.direction == "bullish"]

    def test_current_trend_follows_last_break(self):
        assert current_trend(_df(_trend_bars()), left=2, right=2) == "up"

    def test_max_age_rejects_a_stale_break(self):
        bars = _trend_bars() + [(112, 113, 111, 112)] * 30
        df = _df(bars)
        assert latest_structure_break(df, left=2, right=2) is not None
        assert latest_structure_break(df, max_age_bars=3, left=2, right=2) is None

    def test_insufficient_bars_returns_empty(self):
        assert detect_structure_breaks(_df([(100, 101, 99, 100)] * 4),
                                       left=2, right=2) == []

    def test_causality_a_break_is_never_retroactively_added_or_moved(self):
        """Appending bars must not change breaks already reported.

        `smartmoneyconcepts` fails this: its swing pass deletes prior swings and
        it back-dates flags to `last_positions[-2]`.
        """
        bars = _trend_bars()
        before = detect_structure_breaks(_df(bars), left=2, right=2)
        after = detect_structure_breaks(
            _df(bars + [(112, 160, 111, 159), (159, 170, 158, 169)]),
            left=2, right=2,
        )
        assert len(after) >= len(before)
        for a, b in zip(before, after):
            assert (a.idx, a.kind, a.direction, a.level) == (
                b.idx, b.kind, b.direction, b.level
            )
