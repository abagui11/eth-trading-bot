"""Tests for suggestion traceability validation."""

from __future__ import annotations

import pytest

from analyze import _validate
from models import Suggestion


def test_validate_no_trade_defaults_decision_chart():
    data = {
        "action": "no_trade",
        "size": 0,
        "entry": None,
        "stop_loss": None,
        "take_profits": [],
        "risk_reward": None,
        "rationale": "No setup at Prev Week Mid.",
        "order_block": None,
    }
    s = _validate(data)
    assert s.decision_charts == ["H1"]


def test_validate_trade_requires_structure_and_entry_chart():
    data = {
        "action": "spot_buy",
        "size": 0.5,
        "entry": 2400.0,
        "stop_loss": 2350.0,
        "take_profits": [2500.0],
        "risk_reward": 2.0,
        "rationale": "H12 OB retest.",
        "structure_chart": "H12",
        "entry_chart": "H1",
        "decision_charts": ["H12", "H1"],
        "order_block": {
            "low": 2380.0,
            "high": 2420.0,
            "start_ts": "2026-06-20T12:00:00Z",
            "end_ts": "2026-06-23T08:00:00Z",
        },
    }
    s = _validate(data)
    assert s.structure_chart == "H12"
    assert s.entry_chart == "H1"


def test_validate_trade_rejects_missing_entry_chart():
    data = {
        "action": "spot_buy",
        "size": 0.5,
        "entry": 2400.0,
        "stop_loss": 2350.0,
        "take_profits": [2500.0],
        "risk_reward": 2.0,
        "rationale": "test",
        "structure_chart": "H12",
        "order_block": {
            "low": 2380.0,
            "high": 2420.0,
            "start_ts": "2026-06-20T12:00:00Z",
            "end_ts": "2026-06-23T08:00:00Z",
        },
    }
    with pytest.raises(ValueError, match="entry_chart"):
        _validate(data)
