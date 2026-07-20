"""SFP index rebuild/query tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from patterns import sfp_index
from patterns.sfp import SFPEvent


def _fake_event(ts: str, direction: str = "bullish", outcome: str = "reversal") -> SFPEvent:
    return SFPEvent(
        ts=ts,
        bar_idx=1,
        timeframe="D1",
        direction=direction,  # type: ignore[arg-type]
        swept_level=100.0,
        sweep_depth_pct=0.5,
        aligns_prior_swing=False,
        volume_spike=False,
        outcome_a=outcome,  # type: ignore[arg-type]
        outcome_b=None,
        outcome_c=None,
    )


def test_rebuild_and_count_match_detect(tmp_path: Path, monkeypatch):
    db = tmp_path / "ohlc.db"
    monkeypatch.setattr("config.OHLC_DB", db)

    bars = [{"ts": f"2024-01-{i:02d}T00:00:00Z"} for i in range(1, 6)]
    events = [
        _fake_event("2024-01-02T00:00:00Z", "bullish", "reversal"),
        _fake_event("2024-01-04T00:00:00Z", "bearish", "invalidation"),
    ]

    with (
        patch.object(sfp_index, "_load_bars", return_value=bars),
        patch("patterns.sfp_index.detect_sfps", return_value=events),
        patch.object(sfp_index, "_candle_max_ts", return_value="2024-01-05T00:00:00Z"),
    ):
        summary = sfp_index.rebuild_sfp_index("ETH-USD", "D1", years=4)
        assert summary["event_count"] == 2
        assert sfp_index.count_sfps("ETH-USD", "D1", years=4, ensure=False) == 2
        inv = sfp_index.list_invalidations("ETH-USD", "D1", years=4, limit=10, ensure=False)
        assert len(inv) == 1
        assert inv[0]["direction"] == "bearish"
        assert inv[0]["outcome_a"] == "invalidation"
