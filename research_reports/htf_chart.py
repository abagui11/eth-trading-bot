"""Resolve HTF marked structure chart for research reports (live stack: H4)."""

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


def _snapshot_htf_path() -> str | None:
    snapshot = audit.get_latest_snapshot()
    if not snapshot:
        return None
    marked = snapshot.get("marked_chart_paths") or {}
    for key in ("H4", "H12"):
        path = marked.get(key)
        if path and Path(path).is_file():
            return path
    return None


def render_fresh_htf_chart() -> str:
    """Build a live H4 marked chart (same overlays as the hourly agent)."""
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data = research.get_all_timeframes()
    daily_bars = research.get_daily_bars_for_levels()
    key_levels = compute_key_levels(daily_bars)
    htf_zones = detect_htf_zones(data["H4"])
    market_context = build_market_context(
        data["H4"], data["H1"], data["M5"], daily_bars=daily_bars
    )
    paths = charts.render_marked_charts(
        data,
        key_levels,
        htf_zones,
        cycle_id=f"research_{cycle_id}",
        market_context=market_context,
    )
    return paths["H4"]


def render_fresh_h12_chart() -> str:
    """Alias for older call sites — returns live H4 HTF chart."""
    return render_fresh_htf_chart()


def resolve_htf_chart() -> tuple[str | None, str]:
    """
    Return (chart_path, caption) for the latest H4 structure chart.
    Prefers the most recent hourly audit snapshot; renders live if missing.
    """
    path = _snapshot_htf_path()
    if path:
        snapshot = audit.get_latest_snapshot() or {}
        cycle_id = snapshot.get("cycle_id", "latest")
        label = "H4" if "H4" in Path(path).name else "HTF"
        return path, f"ETH-USD {label} structure — cycle {cycle_id}"

    try:
        path = render_fresh_htf_chart()
        return path, "ETH-USD H4 structure — live refresh"
    except Exception:
        logger.exception("Failed to render research H4 chart")
        return None, ""
