"""Safe chart file resolution for the dashboard."""

from __future__ import annotations

from pathlib import Path

import config

VALID_KINDS = frozenset({"structure", "entry", "outcome", "marked"})
VALID_TFS = frozenset({"H4", "H1", "M5"})


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


def convention_chart_path(cycle_id: str, tf: str, kind: str) -> Path | None:
    """Resolve `{cycle_id}_{tf}_{kind}.png` under CHARTS_DIR."""
    if not cycle_id or tf not in VALID_TFS or kind not in VALID_KINDS:
        return None
    return resolve_chart_path(str(config.CHARTS_DIR / f"{cycle_id}_{tf}_{kind}.png"))


def parse_ledger_chart_paths(chart_path: str | None) -> list[Path]:
    """Split comma-joined ledger chart_path into existing safe Paths."""
    if not chart_path:
        return []
    found: list[Path] = []
    for part in str(chart_path).split(","):
        path = resolve_chart_path(part.strip())
        if path is not None:
            found.append(path)
    return found


def _match_ledger_path(
    paths: list[Path],
    *,
    tf: str,
    keywords: tuple[str, ...],
) -> Path | None:
    tf_u = tf.upper()
    for path in paths:
        name = path.name.upper()
        if tf_u not in name:
            continue
        if all(kw.upper() in name for kw in keywords):
            return path
    return None


def resolve_trade_chart(
    cycle_id: str,
    *,
    kind: str = "marked",
    tf: str = "H4",
    ledger_chart_path: str | None = None,
    marked_chart_paths: dict[str, str] | None = None,
) -> Path | None:
    """
    Resolve a trade journal chart.

    Preference order:
      1. Convention file `{cycle}_{tf}_{kind}.png`
      2. Kind-specific fallbacks (marked dict / ledger CSV)
    """
    kind_n = (kind or "marked").lower()
    tf_n = (tf or "H4").upper()
    if kind_n not in VALID_KINDS:
        kind_n = "marked"
    if tf_n not in VALID_TFS:
        tf_n = "H4"

    by_convention = convention_chart_path(cycle_id, tf_n, kind_n)
    if by_convention is not None:
        return by_convention

    ledger_paths = parse_ledger_chart_paths(ledger_chart_path)
    marked = marked_chart_paths or {}

    if kind_n == "marked":
        path = resolve_chart_path(marked.get(tf_n) or marked.get("H4") or marked.get("H12"))
        if path is not None:
            return path
        return _match_ledger_path(ledger_paths, tf=tf_n, keywords=("MARKED",))

    if kind_n == "outcome":
        # No further fallback — outcome files are generated at close.
        return None

    if kind_n == "structure":
        path = _match_ledger_path(ledger_paths, tf=tf_n, keywords=("STRUCTURE",))
        if path is not None:
            return path
        # Prefer marked HTF as structure stand-in.
        path = resolve_chart_path(marked.get(tf_n) or marked.get("H4") or marked.get("H12"))
        if path is not None:
            return path
        return _match_ledger_path(ledger_paths, tf=tf_n, keywords=("MARKED",))

    if kind_n == "entry":
        path = _match_ledger_path(ledger_paths, tf=tf_n, keywords=("ENTRY",))
        if path is not None:
            return path
        return _match_ledger_path(ledger_paths, tf=tf_n, keywords=("ANNOTATED",))

    return None


def chart_api_url(cycle_id: str, *, kind: str, tf: str) -> str:
    return f"/api/chart/{cycle_id}?kind={kind}&tf={tf}"


def trade_chart_urls(
    cycle_id: str | None,
    *,
    closed: bool,
    ledger_chart_path: str | None = None,
    marked_chart_paths: dict[str, str] | None = None,
) -> dict[str, str | None]:
    """Structure (H4) + execution (M5) URLs for a trade card; prefer outcome when closed."""
    if not cycle_id:
        return {
            "structure_chart_url": None,
            "execution_chart_url": None,
            "thumb_chart_url": None,
        }

    structure_kind = "outcome" if closed else "structure"
    execution_kind = "outcome" if closed else "entry"

    structure_path = resolve_trade_chart(
        cycle_id,
        kind=structure_kind,
        tf="H4",
        ledger_chart_path=ledger_chart_path,
        marked_chart_paths=marked_chart_paths,
    )
    if structure_path is None and closed:
        structure_path = resolve_trade_chart(
            cycle_id,
            kind="structure",
            tf="H4",
            ledger_chart_path=ledger_chart_path,
            marked_chart_paths=marked_chart_paths,
        )
        structure_kind = "structure"

    execution_path = resolve_trade_chart(
        cycle_id,
        kind=execution_kind,
        tf="M5",
        ledger_chart_path=ledger_chart_path,
        marked_chart_paths=marked_chart_paths,
    )
    if execution_path is None and closed:
        execution_path = resolve_trade_chart(
            cycle_id,
            kind="entry",
            tf="M5",
            ledger_chart_path=ledger_chart_path,
            marked_chart_paths=marked_chart_paths,
        )
        execution_kind = "entry"

    structure_url = (
        chart_api_url(cycle_id, kind=structure_kind, tf="H4") if structure_path else None
    )
    execution_url = (
        chart_api_url(cycle_id, kind=execution_kind, tf="M5") if execution_path else None
    )
    return {
        "structure_chart_url": structure_url,
        "execution_chart_url": execution_url,
        "thumb_chart_url": execution_url or structure_url,
    }


def h4_marked_path(marked: dict[str, str] | None) -> Path | None:
    if not marked:
        return None
    return resolve_chart_path(marked.get("H4") or marked.get("H12"))


def h12_marked_path(marked: dict[str, str] | None) -> Path | None:
    """Alias for ``h4_marked_path`` (historical name)."""
    return h4_marked_path(marked)


def outcome_filenames(cycle_id: str) -> dict[str, str]:
    """Canonical outcome PNG names (relative to CHARTS_DIR)."""
    return {
        "H4": f"{cycle_id}_H4_outcome.png",
        "M5": f"{cycle_id}_M5_outcome.png",
    }
