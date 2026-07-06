"""Tests for H12 HTF zones (order blocks and breakers)."""

from __future__ import annotations

from patterns.htf_structure import detect_htf_zones


def _bar(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def _bullish_msb_series() -> list[dict]:
    bars: list[dict] = []
    for i in range(10):
        bars.append(_bar(f"2026-01-{i+1:02d}T00:00:00Z", 50, 52, 48, 50))
    bars.extend([
        _bar("2026-01-11T00:00:00Z", 50, 58, 49, 57),
        _bar("2026-01-12T00:00:00Z", 57, 60, 56, 59),
        _bar("2026-01-13T00:00:00Z", 59, 70, 58, 69),
        _bar("2026-01-14T00:00:00Z", 69, 69, 60, 61),
        _bar("2026-01-15T00:00:00Z", 61, 62, 55, 56),
        _bar("2026-01-16T00:00:00Z", 56, 57, 52, 53),
        _bar("2026-01-17T00:00:00Z", 53, 72, 52, 71),
    ])
    return bars


def test_detect_htf_zones_finds_bullish_ob():
    zones = detect_htf_zones(_bullish_msb_series(), lookback=15)
    ob_zones = [z for z in zones if z.zone_type == "order_block"]
    assert len(ob_zones) >= 1
    assert any(z.direction == "bullish" for z in ob_zones)
    assert ob_zones[-1].start_ts == "2026-01-16T00:00:00Z"
    assert ob_zones[-1].start_ts != ob_zones[-1].msb_ts


def test_msb_does_not_fire_on_wick_only():
    bars = _bullish_msb_series()
    bars[-1] = _bar("2026-01-17T00:00:00Z", 53, 75, 52, 68)
    zones = detect_htf_zones(bars, lookback=15)
    assert not any(z.msb_ts == "2026-01-17T00:00:00Z" for z in zones)


def test_mitigation_sets_end_ts():
    bars = _bullish_msb_series()
    bars.append(_bar("2026-01-18T00:00:00Z", 71, 72, 50, 51))
    zones = detect_htf_zones(bars, lookback=20)
    mitigated = [z for z in zones if z.mitigated and z.end_ts]
    assert len(mitigated) >= 1


def _breaker_series() -> list[dict]:
    """Bearish OB violated then bullish MSB -> bullish breaker."""
    bars = _bullish_msb_series()
    bars.extend([
        _bar("2026-01-18T00:00:00Z", 71, 72, 60, 66),
        _bar("2026-01-19T00:00:00Z", 66, 67, 55, 59),
        _bar("2026-01-20T00:00:00Z", 59, 60, 52, 56),
        _bar("2026-01-21T00:00:00Z", 56, 58, 52, 55),
        _bar("2026-01-22T00:00:00Z", 55, 56, 44, 45),
        _bar("2026-01-23T00:00:00Z", 45, 58, 45, 57),
        _bar("2026-01-24T00:00:00Z", 57, 58, 45, 40),
        _bar("2026-01-25T00:00:00Z", 40, 62, 40, 60),
        _bar("2026-01-26T00:00:00Z", 60, 75, 59, 72),
    ])
    return bars


def test_breaker_after_opposite_msb():
    zones = detect_htf_zones(_breaker_series(), lookback=35)
    assert any(z.zone_type == "breaker" and z.direction == "bullish" for z in zones)


def test_promoted_ob_excluded_when_breaker_exists():
    """Mitigated OB that became a breaker must not appear alongside it."""
    zones = detect_htf_zones(_breaker_series(), lookback=35)
    breakers = [z for z in zones if z.zone_type == "breaker" and z.direction == "bullish"]
    assert breakers
    for breaker in breakers:
        superseded = [
            z
            for z in zones
            if z.zone_type == "order_block"
            and z.start_ts == breaker.start_ts
            and z.low == breaker.low
            and z.high == breaker.high
        ]
        assert superseded == []


def test_empty_bars_returns_empty():
    assert detect_htf_zones([]) == []
