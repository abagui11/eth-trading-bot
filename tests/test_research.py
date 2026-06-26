"""Research output safety checks."""

from __future__ import annotations

import re

from analytics import _build_caption, _format_summary
from patterns.sfp import SFPEvent, compute_stats

_SECRET_PATTERNS = (
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"ANTHROPIC_API_KEY"),
    re.compile(r"TELEGRAM_BOT_TOKEN"),
)


def _assert_no_secrets(text: str) -> None:
    for pattern in _SECRET_PATTERNS:
        assert not pattern.search(text), f"secret-like value in outbound text: {pattern.pattern}"


def test_summary_and_caption_contain_no_secrets():
    events = [
        SFPEvent(
            ts="2026-01-01T00:00:00Z",
            bar_idx=0,
            timeframe="W1",
            direction="bullish",
            swept_level=2000.0,
            sweep_depth_pct=0.5,
            aligns_prior_swing=False,
            volume_spike=False,
            outcome_a="reversal",
            outcome_b=True,
            outcome_c=False,
        )
    ]
    stats = compute_stats(events)
    summary = _format_summary(stats, years=4, events=events, timeframe="W1", bar_count=200)
    caption = _build_caption(stats, years=4, timeframe="W1")
    _assert_no_secrets(summary)
    _assert_no_secrets(caption)


def test_compute_stats_bc_denominator_matches_counts():
    events = [
        SFPEvent(
            ts="2026-01-01T00:00:00Z",
            bar_idx=0,
            timeframe="H12",
            direction="bearish",
            swept_level=3000.0,
            sweep_depth_pct=0.3,
            aligns_prior_swing=True,
            volume_spike=True,
            outcome_a="neutral",
            outcome_b=False,
            outcome_c=True,
        ),
        SFPEvent(
            ts="2026-06-01T00:00:00Z",
            bar_idx=10,
            timeframe="H12",
            direction="bullish",
            swept_level=2500.0,
            sweep_depth_pct=0.4,
            aligns_prior_swing=False,
            volume_spike=False,
            outcome_a="pending",
            outcome_b=None,
            outcome_c=None,
        ),
    ]
    stats = compute_stats(events)
    assert stats["neutral"] == 1
    assert stats["pending"] == 1
    assert stats["outcome_bc_eligible"] == 1
    assert stats["outcome_b_count"] == 0
    assert stats["outcome_c_count"] == 1
    assert stats["outcome_b_pct"] == 0.0
    assert stats["outcome_c_pct"] == 100.0

    summary = _format_summary(stats, years=4, events=events, timeframe="H12", bar_count=100)
    assert "(0/1)" in summary
    assert "(1/1)" in summary
