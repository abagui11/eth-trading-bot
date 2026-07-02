"""Tests for chat snapshot grounding."""

from __future__ import annotations

from unittest.mock import patch

import chat


def test_build_context_includes_snapshot_summary():
    snapshot = {
        "cycle_id": "20260702T120000Z",
        "snapshot": {
            "spot": 1615.0,
            "summary_text": "=== Programmatic market context ===\nCurrent spot: $1,615.00",
            "alerts": [],
            "h12_sfps": [],
            "h1_sfps": [],
            "live_invalidated_sfps": [],
            "order_blocks": [],
            "htf_zones": [],
            "key_levels_near": [],
            "setup_tags": [],
            "is_ranging": False,
            "range_break": None,
        },
        "marked_chart_paths": {"H12": "/tmp/fake_h12.png"},
    }

    with patch("chat.audit.get_latest_snapshot", return_value=snapshot):
        text, _chart_path, snapshot_charts = chat._build_context(1615.0, "What is the bias?")

    assert "Authoritative cycle snapshot" in text
    assert "Programmatic market context" in text
    assert snapshot_charts.get("H12") == "/tmp/fake_h12.png"
