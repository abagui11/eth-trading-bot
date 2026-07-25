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
    system = client.messages.create.call_args_list[0].kwargs["system"]
    assert isinstance(system, list)
    assert "How to read the marked live charts" in system[0]["text"]
    assert system[0].get("cache_control", {}).get("type") == "ephemeral"


def test_build_user_content_skips_pattern_images_by_default(tmp_path, monkeypatch):
    import bot_config
    from analyze import _build_user_content

    monkeypatch.setattr(bot_config, "INCLUDE_PATTERN_IMAGES", False)
    chart = tmp_path / "h4.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    paths = {"H4": str(chart), "H1": str(chart), "M5": str(chart)}

    with patch("analyze.load_pattern_images") as mock_patterns:
        content = _build_user_content(paths)
        mock_patterns.assert_not_called()

    texts = [b["text"] for b in content if b.get("type") == "text"]
    assert not any("Reference pattern examples" in t for t in texts)
    assert not any(b.get("type") == "image" and "Reference" in str(b) for b in content)
    assert sum(1 for b in content if b.get("type") == "image") == 3


def test_build_user_content_includes_patterns_when_enabled(tmp_path, monkeypatch):
    import bot_config
    from analyze import _build_user_content
    from pathlib import Path

    monkeypatch.setattr(bot_config, "INCLUDE_PATTERN_IMAGES", True)
    chart = tmp_path / "h4.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    pattern = tmp_path / "pattern.png"
    pattern.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    paths = {"H4": str(chart), "H1": str(chart), "M5": str(chart)}

    with patch(
        "analyze.load_pattern_images",
        return_value=[("example (pattern.png)", Path(pattern))],
    ):
        content = _build_user_content(paths)

    texts = [b["text"] for b in content if b.get("type") == "text"]
    assert any("Reference pattern examples" in t for t in texts)
    assert sum(1 for b in content if b.get("type") == "image") == 4


def test_build_vision_content_defaults_to_config(monkeypatch):
    import bot_config
    from analyze import build_vision_content

    monkeypatch.setattr(bot_config, "INCLUDE_PATTERN_IMAGES", False)
    with patch("analyze.load_pattern_images") as mock_patterns:
        content = build_vision_content(include_live_charts=False, include_patterns=None)
        mock_patterns.assert_not_called()
    assert content == []
