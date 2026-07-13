"""Read-only data accessors for the dashboard API."""

from __future__ import annotations

import time
from typing import Any

import audit
import ledger
import paper
import research

from dashboard.charts import h4_marked_path
from dashboard.performance import build_performance, _score_badge
from dashboard.status import format_agent_status
from macro.context import macro_payload_for_dashboard

_spot_cache: tuple[float, float] = (0.0, 0.0)
_SPOT_TTL_SEC = 30.0


def get_live_spot() -> dict[str, Any]:
    global _spot_cache
    now = time.time()
    if now - _spot_cache[1] > _SPOT_TTL_SEC or _spot_cache[0] <= 0:
        price = research.get_spot_price()
        _spot_cache = (price, now)
    return {"spot": _spot_cache[0], "ts": int(_spot_cache[1])}


def get_status_payload() -> dict[str, Any]:
    spot = get_live_spot()["spot"]
    snapshot = audit.get_latest_snapshot()
    latest_ledger = ledger.get_latest_suggestion()
    positions = paper.get_open_positions(spot)
    status = format_agent_status(
        snapshot,
        ledger_row=latest_ledger,
        open_positions=positions,
    )
    verdict = None
    if status.get("cycle_id"):
        verdict = audit.get_verdict_by_cycle_id(str(status["cycle_id"]))
    chart_path = h4_marked_path((snapshot or {}).get("marked_chart_paths"))
    return {
        **status,
        "spot": spot,
        "chart_read_score": verdict.get("score") if verdict else None,
        "score_badge": _score_badge(verdict.get("score") if verdict else None),
        "h4_chart_url": (
            f"/api/chart/{status['cycle_id']}" if chart_path and status.get("cycle_id") else None
        ),
    }


def get_cycles(limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
    rows = ledger.get_latest(limit + offset)
    if offset:
        rows = rows[offset:]
    else:
        rows = rows[:limit]
    results: list[dict[str, Any]] = []
    for row in rows:
        cycle_id = str(row.get("cycle_id") or "")
        verdict = audit.get_verdict_by_cycle_id(cycle_id) if cycle_id else None
        score = verdict.get("score") if verdict else None
        results.append(
            {
                "id": row.get("id"),
                "ts": row.get("ts"),
                "cycle_id": cycle_id,
                "action": row.get("action"),
                "price_at_suggestion": row.get("price_at_suggestion"),
                "risk_reward": row.get("risk_reward"),
                "setup_tags": row.get("setup_tags"),
                "chart_read_score": score,
                "score_badge": _score_badge(score),
                "has_issues": verdict.get("has_issues") if verdict else None,
                "rationale_excerpt": _excerpt(str(row.get("rationale") or ""), 160),
            }
        )
    return results


def get_cycle_detail(cycle_id: str) -> dict[str, Any] | None:
    row = ledger.get_suggestion_by_cycle_id(cycle_id)
    if row is None:
        return None
    snapshot = audit.get_snapshot(cycle_id)
    verdict = audit.get_verdict_by_cycle_id(cycle_id)
    marked = (snapshot or {}).get("marked_chart_paths") or {}
    return {
        "ledger": row,
        "snapshot": (snapshot or {}).get("snapshot"),
        "suggestion": (snapshot or {}).get("suggestion"),
        "verdict": verdict,
        "h4_chart_url": f"/api/chart/{cycle_id}" if h4_marked_path(marked) else None,
    }


def get_open_positions_payload() -> list[dict[str, Any]]:
    spot = get_live_spot()["spot"]
    return paper.get_open_positions(spot)


def get_closed_trades_payload(limit: int = 50) -> list[dict[str, Any]]:
    return paper.get_closed_trades(limit=limit)


def get_archived_trades_payload(limit: int = 50) -> list[dict[str, Any]]:
    return paper.get_archived_closed_trades(limit=limit)


def get_performance_payload() -> dict[str, Any]:
    spot = get_live_spot()["spot"]
    return build_performance(spot)


def get_macro_payload() -> dict[str, Any]:
    return macro_payload_for_dashboard()


def _excerpt(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "..."
