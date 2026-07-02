"""Safe chart file resolution for the dashboard."""

from __future__ import annotations

from pathlib import Path

import config


def resolve_chart_path(raw: str | None) -> Path | None:
    """Return an absolute path under CHARTS_DIR, or None if invalid/missing."""
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = config.ROOT_DIR / candidate
    try:
        resolved = candidate.resolve()
        charts_root = config.CHARTS_DIR.resolve()
        resolved.relative_to(charts_root)
    except (ValueError, OSError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def h12_marked_path(marked: dict[str, str] | None) -> Path | None:
    if not marked:
        return None
    return resolve_chart_path(marked.get("H12"))
