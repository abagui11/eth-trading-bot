"""Tests for key-level label staggering on charts."""

from __future__ import annotations

from charts import _plan_key_level_labels
from patterns.key_levels import KeyLevel


def _lv(label: str, price: float, color: str = "#D4AF37") -> KeyLevel:
    return KeyLevel(price=price, label=label, color=color)


def test_plan_key_level_labels_alternates_sides_when_clustered():
    levels = [
        _lv("Monday High", 1635.0, "#ffffff"),
        _lv("Daily Open", 1610.6, "#08bcd4"),
        _lv("Monday Mid", 1591.38, "#ffffff"),
        _lv("Weekly Open", 1569.4, "#D4AF37"),
        _lv("Monday Low", 1547.77, "#ffffff"),
    ]
    planned = _plan_key_level_labels(levels, y_lo=1500.0, y_hi=1700.0)
    sides = [side for _, _, side in planned]
    assert "left" in sides
    assert "right" in sides
    assert len({round(y, 2) for _, y, _ in planned}) == len(planned)


def test_plan_key_level_labels_keeps_sparse_levels_on_right():
    levels = [_lv("Prev Week Low", 1510.0), _lv("Prev Week High", 1777.83)]
    planned = _plan_key_level_labels(levels, y_lo=1500.0, y_hi=1800.0)
    assert all(side == "right" for _, _, side in planned)
    assert all(abs(y - lv.price) < 1.0 for (lv, y, _) in planned)
