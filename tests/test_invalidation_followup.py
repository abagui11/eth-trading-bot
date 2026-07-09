"""Post-invalidation follow-up scoring tests."""

from __future__ import annotations

import pandas as pd

from patterns.invalidation_followup import (
    compute_invalidation_stats,
    find_invalidation_bar,
    score_post_invalidation,
)
from patterns.sfp import SFPEvent


def _bars_from_closes(closes: list[float]) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        rows.append(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 100.0,
            }
        )
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="12h", tz="UTC")
    return pd.DataFrame(rows, index=idx)


def _event(bar_idx: int, direction: str, level: float) -> SFPEvent:
    return SFPEvent(
        ts="2026-01-01T00:00:00Z",
        bar_idx=bar_idx,
        timeframe="H12",
        direction=direction,
        swept_level=level,
        sweep_depth_pct=0.5,
        aligns_prior_swing=False,
        volume_spike=False,
        outcome_a="invalidation",
        outcome_b=None,
        outcome_c=None,
    )


def test_find_invalidation_bar_bearish():
    # Bearish SFP at 100, swept 105; bar 2 closes above 105
    df = _bars_from_closes([100, 104, 106, 108, 110, 112, 114, 116, 118, 120, 122, 124, 126, 128, 130, 132])
    inv = find_invalidation_bar(df, 0, "bearish", 105.0, 14)
    assert inv == 2


def test_score_post_invalidation_continuation_bearish():
    df = _bars_from_closes([100, 104, 106, 110, 115, 120, 125, 130, 135, 140, 145, 150, 155, 160, 165, 170])
    event = _event(0, "bearish", 105.0)
    fu = score_post_invalidation(df, event, post_n_bars=10)
    assert fu.outcome == "continuation"
    assert fu.invalidation_bar_idx == 2


def test_compute_invalidation_stats():
    events = [
        score_post_invalidation(
            _bars_from_closes([100, 104, 106, 110, 115, 120, 125, 130, 135, 140, 145, 150, 155, 160, 165, 170]),
            _event(0, "bearish", 105.0),
            post_n_bars=8,
        )
    ]
    stats = compute_invalidation_stats(events)
    assert stats["total"] == 1
    assert stats["continuation"] == 1
