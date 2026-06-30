"""Tests for SpacemanBTC-style key levels."""

from __future__ import annotations

from datetime import datetime, timezone

from patterns.key_levels import KeyLevel, compute_key_levels, merge_levels


def _daily(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def test_merge_levels_combines_same_price():
    levels = [
        KeyLevel(100.0, "Weekly Open", "#fffcbc"),
        KeyLevel(100.0, "Daily Open", "#08bcd4"),
    ]
    merged = merge_levels(levels, tolerance_pct=0.0001)
    assert len(merged) == 1
    assert "Weekly Open" in merged[0].label
    assert "Daily Open" in merged[0].label


def test_monday_range_from_current_week():
    # Distinct price levels to avoid Pine-style merge collisions in sparse fixtures.
    bars = [
        _daily("2026-06-22T00:00:00Z", 500, 510, 490, 505),
        _daily("2026-06-29T00:00:00Z", 1000, 1100, 950, 1050),
        _daily("2026-06-30T00:00:00Z", 1050, 1080, 1020, 1060),
        _daily("2026-07-01T00:00:00Z", 1060, 1120, 1040, 1100),
    ]
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    levels = compute_key_levels(bars, now=now)
    labels = {lv.label: lv.price for lv in levels}
    monday_high = next((lv.price for lv in levels if lv.label == "Monday High" or "Monday High" in lv.label), None)
    monday_low = next((lv.price for lv in levels if lv.label == "Monday Low" or "Monday Low" in lv.label), None)
    assert monday_high == 1100.0
    assert monday_low == 950.0
    assert "Monday Mid" in labels or any("Monday Mid" in lv.label for lv in levels)


def test_prev_week_high_low_mid():
    bars = [
        _daily("2026-06-16T00:00:00Z", 200, 210, 180, 205),
        _daily("2026-06-23T00:00:00Z", 300, 350, 280, 340),
        _daily("2026-06-24T00:00:00Z", 340, 360, 330, 355),
        _daily("2026-06-30T00:00:00Z", 1000, 1050, 980, 1030),
        _daily("2026-07-01T00:00:00Z", 1030, 1060, 1010, 1040),
    ]
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    levels = compute_key_levels(bars, now=now)
    pwh = next(lv.price for lv in levels if "Prev Week High" in lv.label)
    pwl = next(lv.price for lv in levels if "Prev Week Low" in lv.label)
    assert pwh == 360.0
    assert pwl == 280.0


def test_monthly_open_and_prev_month_levels():
    bars = [
        _daily("2026-05-01T00:00:00Z", 200, 300, 190, 280),
        _daily("2026-05-15T00:00:00Z", 280, 320, 270, 310),
        _daily("2026-06-01T00:00:00Z", 1000, 1100, 990, 1080),
        _daily("2026-06-15T00:00:00Z", 1080, 1150, 1070, 1120),
    ]
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    levels = compute_key_levels(bars, now=now)
    mo = next(lv.price for lv in levels if lv.label == "Monthly Open")
    pmh = next(lv.price for lv in levels if "Prev Month High" in lv.label)
    assert mo == 1000.0
    assert pmh == 320.0
