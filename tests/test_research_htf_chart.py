"""HTF chart resolution for research digest."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from research_reports.htf_chart import resolve_htf_chart


def test_resolve_htf_chart_uses_snapshot_when_available(tmp_path, monkeypatch):
    chart = tmp_path / "h4.png"
    chart.write_bytes(b"png")

    snapshot = {
        "cycle_id": "20260709T120000Z",
        "marked_chart_paths": {"H4": str(chart)},
    }

    with patch("research_reports.htf_chart.audit.get_latest_snapshot", return_value=snapshot):
        path, caption = resolve_htf_chart()

    assert path == str(chart)
    assert "20260709T120000Z" in caption


def test_resolve_htf_chart_accepts_legacy_h12_snapshot(tmp_path):
    chart = tmp_path / "h12.png"
    chart.write_bytes(b"png")

    snapshot = {
        "cycle_id": "20260709T120000Z",
        "marked_chart_paths": {"H12": str(chart)},
    }

    with patch("research_reports.htf_chart.audit.get_latest_snapshot", return_value=snapshot):
        path, caption = resolve_htf_chart()

    assert path == str(chart)
    assert "20260709T120000Z" in caption


def test_resolve_htf_chart_renders_when_snapshot_missing(monkeypatch):
    with patch("research_reports.htf_chart.audit.get_latest_snapshot", return_value=None):
        with patch(
            "research_reports.htf_chart.render_fresh_htf_chart",
            return_value="/tmp/research_h4.png",
        ):
            path, caption = resolve_htf_chart()

    assert path == "/tmp/research_h4.png"
    assert "live refresh" in caption
