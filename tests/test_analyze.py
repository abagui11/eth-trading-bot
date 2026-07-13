"""Tests for suggestion traceability validation."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from analyze import MAX_SUGGESTION_TOKENS, _validate, propose_trade
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
    assert s.decision_charts == ["H4"]


def test_validate_trade_requires_structure_and_entry_chart():
    data = {
        "action": "spot_buy",
        "size": 0.5,
        "entry": 2408.0,
        "stop_loss": 2350.0,
        "take_profits": [2500.0],
        "risk_reward": 2.0,
        "rationale": "H4 OB retest.",
        "structure_chart": "H4",
        "entry_chart": "M5",
        "decision_charts": ["H4", "M5"],
        "order_block": {
            "low": 2380.0,
            "high": 2420.0,
            "start_ts": "2026-06-20T12:00:00Z",
            "end_ts": "2026-06-20T12:00:00Z",
        },
    }
    s = _validate(data)
    assert s.structure_chart == "H4"
    assert s.entry_chart == "M5"
    assert s.risk_reward == pytest.approx(92 / 58, rel=1e-3)


def test_validate_trade_defaults_missing_entry_chart_to_m5():
    data = {
        "action": "spot_buy",
        "size": 0.5,
        "entry": 2408.0,
        "stop_loss": 2350.0,
        "take_profits": [2500.0],
        "risk_reward": 2.0,
        "rationale": "test",
        "structure_chart": "H4",
        "order_block": {
            "low": 2380.0,
            "high": 2420.0,
            "start_ts": "2026-06-20T12:00:00Z",
            "end_ts": "2026-06-20T12:00:00Z",
        },
    }
    s = _validate(data)
    assert s.entry_chart == "M5"


def test_propose_trade_retries_on_json_decode_error():
    valid_payload = {
        "action": "no_trade",
        "size": 0,
        "entry": None,
        "stop_loss": None,
        "take_profits": [],
        "risk_reward": None,
        "rationale": "No setup.",
        "order_block": None,
        "decision_charts": ["H4"],
    }
    bad_block = MagicMock()
    bad_block.type = "text"
    bad_block.text = '{"action": "no_trade", "rationale": "unterminated'
    good_block = MagicMock()
    good_block.type = "text"
    good_block.text = json.dumps(valid_payload)

    response_bad = MagicMock()
    response_bad.content = [bad_block]
    response_good = MagicMock()
    response_good.content = [good_block]

    client = MagicMock()
    client.messages.create.side_effect = [response_bad, response_good]

    with patch("analyze.anthropic.Anthropic", return_value=client), patch(
        "analyze._build_user_content", return_value=[{"type": "text", "text": "test"}]
    ):
        suggestion = propose_trade({"H4": "x.png", "H1": "y.png", "M5": "z.png"})

    assert suggestion.action == "no_trade"
    assert client.messages.create.call_count == 2
    assert MAX_SUGGESTION_TOKENS == 1536
