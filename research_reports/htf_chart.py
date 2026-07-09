"""Resolve H12 marked structure chart for research reports."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import audit
import charts
import research
from patterns.htf_structure import detect_htf_zones
from patterns.key_levels import compute_key_levels
from patterns.market_context import build_market_context

logger = logging.getLogger(__name__)


def _snapshot_h12_path() -> str | None:
    snapshot = audit.get_latest_snapshot()
    if not snapshot:
        return None
    marked = snapshot.get("marked_chart_paths") or {}
    path = marked.get("H12")
    if path and Path(path).is_file():
        return path
    return None


def render_fresh_h12_chart() -> str:
    """Build a live H12 marked chart (same overlays as the hourly agent)."""
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data = research.get_all_timeframes()
    daily_bars = research.get_daily_bars_for_levels()
    key_levels = compute_key_levels(daily_bars)
    htf_zones = detect_htf_zones(data["H12"])
    market_context = build_market_context(
        data["H12"], data["H4"], data["H1"], daily_bars=daily_bars
    )
    paths = charts.render_marked_charts(
        data,
        key_levels,
        htf_zones,
        cycle_id=f"research_{cycle_id}",
        market_context=market_context,
    )
    return paths["H12"]


def resolve_htf_chart() -> tuple[str | None, str]:
    """
    Return (chart_path, caption) for the latest H12 structure chart.
    Prefers the most recent hourly audit snapshot; renders live if missing.
    """
    path = _snapshot_h12_path()
    if path:
        snapshot = audit.get_latest_snapshot() or {}
        cycle_id = snapshot.get("cycle_id", "latest")
        return path, f"ETH-USD H12 structure — cycle {cycle_id}"

    try:
        path = render_fresh_h12_chart()
        return path, "ETH-USD H12 structure — live refresh"
    except Exception:
        logger.exception("Failed to render research H12 chart")
        return None, ""
