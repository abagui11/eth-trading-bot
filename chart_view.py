"""Latest chart + watch summary for user chart requests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import audit
import config
import ledger
import research


@dataclass
class ChartView:
    cycle_id: str
    chart_paths: list[str]
    caption: str
    watch_summary: str


def _existing_paths(*candidates: str | None) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        for part in candidate.split(","):
            part = part.strip()
            if not part or part in seen:
                continue
            if Path(part).exists():
                seen.add(part)
                paths.append(part)
    return paths


def _paths_from_marked(marked: dict[str, str], *, prefer: list[str] | None = None) -> list[str]:
    order = prefer or ["H4", "H1", "M5"]
    paths: list[str] = []
    for tf in order:
        path = marked.get(tf)
        if path and Path(path).exists() and path not in paths:
            paths.append(path)
    return paths


def _latest_chart_files() -> list[str]:
    patterns = ("*_entry.png", "*_structure.png", "*_notrade.png", "*_marked.png")
    found: list[Path] = []
    for pattern in patterns:
        found.extend(config.CHARTS_DIR.glob(pattern))
    if not found:
        return []
    newest = max(found, key=lambda p: p.stat().st_mtime)
    return [str(newest)]


def _resolve_chart_paths(
    ledger_row: dict,
    snapshot_row: dict | None,
) -> list[str]:
    output_paths = _existing_paths(ledger_row.get("chart_path"))
    if output_paths:
        return output_paths[:2]

    marked = (snapshot_row or {}).get("marked_chart_paths") or {}
    suggestion = (snapshot_row or {}).get("suggestion") or {}
    decision = suggestion.get("decision_charts") or []
    marked_paths = _paths_from_marked(marked, prefer=list(decision) + ["H4", "H1", "M5"])
    if marked_paths:
        return marked_paths[:2]

    return _latest_chart_files()


def _format_zone(zone: dict) -> str:
    zone_type = str(zone.get("zone_type", "zone")).upper()
    direction = str(zone.get("direction", ""))
    low = float(zone["low"])
    high = float(zone["high"])
    return f"{zone_type} {direction} {low:,.2f}-{high:,.2f}"


def _format_ob(ob: dict) -> str:
    direction = str(ob.get("direction", ""))
    low = float(ob["low"])
    high = float(ob["high"])
    return f"M5 {direction} OB {low:,.2f}-{high:,.2f}"


def _format_sfp(event: dict) -> str:
    tf = str(event.get("timeframe", ""))
    direction = str(event.get("direction", ""))
    level = float(event["swept_level"])
    outcome = str(event.get("outcome_a", ""))
    return f"{tf} {direction} SFP @ {level:,.2f} ({outcome})"


def _build_watch_summary(ledger_row: dict, snapshot_row: dict | None) -> str:
    cycle_id = str(ledger_row["cycle_id"])
    action = str(ledger_row.get("action") or "n/a")
    ts = str(ledger_row.get("ts") or "")
    lines = [
        f"Latest analysis — cycle {cycle_id}",
        f"Time: {ts} | Action: {action}",
    ]

    if snapshot_row:
        snap = snapshot_row.get("snapshot") or {}
        spot = float(snap.get("spot") or ledger_row.get("price_at_suggestion") or 0.0)
        lines.append(f"Spot at cycle: ${spot:,.2f}")

        range_24h = snap.get("range_24h")
        if range_24h:
            high = float(range_24h["high"])
            low = float(range_24h["low"])
            width = float(range_24h.get("width_pct") or 0.0)
            ranging = "ranging" if range_24h.get("is_ranging") else "trending"
            break_note = ""
            if snap.get("range_break"):
                break_note = f" | break {snap['range_break']}"
            lines.append(f"24h range: {low:,.2f}-{high:,.2f} ({width:.2f}% {ranging}){break_note}")

        zone_snap = snap.get("zone_snapshot") or {}
        zones = zone_snap.get("zones_containing_price") or []
        if zones:
            lines.append("HTF zones at price:")
            for zone in zones[:3]:
                lines.append(f"  • {_format_zone(zone)}")

        order_blocks = snap.get("order_blocks") or []
        if order_blocks:
            lines.append("M5 order blocks on chart:")
            for ob in order_blocks[-3:]:
                lines.append(f"  • {_format_ob(ob)}")
        else:
            lines.append("M5 order blocks: none in lookback")

        h4_sfps = snap.get("h4_sfps") or snap.get("h12_sfps") or []
        m5_sfps = snap.get("m5_sfps") or snap.get("h1_sfps") or []
        if h4_sfps or m5_sfps:
            lines.append("Recent SFPs:")
            for event in (h4_sfps + m5_sfps)[:4]:
                lines.append(f"  • {_format_sfp(event)}")

        key_levels = snap.get("key_levels_near") or []
        if key_levels:
            labels = ", ".join(
                f"{lv.get('label')} @ {float(lv['price']):,.2f}" for lv in key_levels[:4]
            )
            lines.append(f"Key levels near spot: {labels}")

        setup_state = snap.get("setup_state")
        if setup_state and setup_state.get("phase"):
            lines.append(f"Setup state: {setup_state['phase']}")

        alerts = snap.get("alerts") or []
        if alerts:
            lines.append("Alerts:")
            for alert in alerts[:5]:
                lines.append(f"  • {alert}")

        setup_tags = snap.get("setup_tags") or []
        if setup_tags:
            lines.append(f"Tags: {', '.join(setup_tags)}")
    else:
        spot = research.get_spot_price()
        lines.append(f"Current spot: ${spot:,.2f}")
        tags = ledger_row.get("setup_tags")
        if tags:
            lines.append(f"Tags: {tags}")
        rationale = str(ledger_row.get("rationale") or "").strip()
        if rationale:
            excerpt = rationale.replace("\n", " ")
            if len(excerpt) > 500:
                excerpt = excerpt[:500].rstrip() + "..."
            lines.append(f"Rationale excerpt: {excerpt}")

    lines.append("")
    lines.append("Watching for M5 fib retests, H4 structure, 24h range breaks, and fresh SFPs.")
    return "\n".join(lines)


def _build_caption(ledger_row: dict, chart_paths: list[str]) -> str:
    cycle_id = str(ledger_row["cycle_id"])
    action = str(ledger_row.get("action") or "n/a").upper()
    primary = Path(chart_paths[0]).stem if chart_paths else "chart"
    tf = ""
    for token in ("H4", "H1", "M5", "H12"):
        if token in primary:
            tf = token
            break
    label = f"ETH-USD {tf}" if tf else "ETH-USD"
    return f"{label} — {action}\nCycle {cycle_id}"


def get_latest_chart_view() -> ChartView | None:
    """Return the latest cycle chart(s) and a concise watch summary."""
    ledger_row = ledger.get_latest_suggestion()
    if ledger_row is None:
        return None

    cycle_id = str(ledger_row["cycle_id"])
    snapshot_row = audit.get_snapshot(cycle_id) or audit.get_latest_snapshot()
    chart_paths = _resolve_chart_paths(ledger_row, snapshot_row)
    if not chart_paths:
        return None

    return ChartView(
        cycle_id=cycle_id,
        chart_paths=chart_paths,
        caption=_build_caption(ledger_row, chart_paths),
        watch_summary=_build_watch_summary(ledger_row, snapshot_row),
    )
