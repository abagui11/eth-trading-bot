"""Tests for chat snapshot grounding."""

from __future__ import annotations

from unittest.mock import patch

import analyze
import chat


def test_build_context_includes_snapshot_summary():
    snapshot = {
        "cycle_id": "20260702T120000Z",
        "snapshot": {
            "spot": 1615.0,
            "summary_text": "=== Programmatic market context ===\nCurrent spot: $1,615.00",
            "alerts": [],
            "h4_sfps": [],
            "m5_sfps": [],
            "live_invalidated_sfps": [],
            "order_blocks": [],
            "htf_zones": [],
            "key_levels_near": [],
            "setup_tags": [],
            "is_ranging": False,
            "range_break": None,
        },
        "marked_chart_paths": {"H4": "/tmp/fake_h4.png"},
    }

    with patch("chat.audit.get_latest_snapshot", return_value=snapshot):
        text, _chart_path, snapshot_charts = chat._build_context(1615.0, "What is the bias?")

    assert "Authoritative cycle snapshot" in text
    assert "Programmatic market context" in text
    assert snapshot_charts.get("H4") == "/tmp/fake_h4.png"


def test_build_vision_content_skips_missing_h1(tmp_path, monkeypatch):
    """Chat passes H4/M5 only; build_vision_content must not KeyError on H1."""
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"png")

    monkeypatch.setattr(analyze, "_encode_image", lambda _path: "base64")

    blocks = analyze.build_vision_content(
        chart_paths={"H4": str(chart), "M5": str(chart)},
        include_patterns=False,
    )
    labels = [b["text"] for b in blocks if b.get("type") == "text"]
    assert "--- Live H4 chart ---" in labels
    assert "--- Live M5 chart ---" in labels
    assert "--- Live H1 chart ---" not in labels
