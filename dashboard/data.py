"""Read-only data accessors for the dashboard API."""

from __future__ import annotations

import json
import time
from typing import Any

import audit
import ledger
import paper
import research

from dashboard.charts import h4_marked_path, trade_chart_urls
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
    return [enrich_open_position(pos) for pos in paper.get_open_positions(spot)]


def get_closed_trades_payload(limit: int = 50) -> list[dict[str, Any]]:
    return [enrich_closed_trade(t) for t in paper.get_closed_trades(limit=limit)]


def get_archived_trades_payload(limit: int = 50) -> list[dict[str, Any]]:
    return [
        enrich_closed_trade(t, status="archived")
        for t in paper.get_archived_closed_trades(limit=limit)
    ]


def get_performance_payload() -> dict[str, Any]:
    spot = get_live_spot()["spot"]
    return build_performance(spot)


def get_macro_payload() -> dict[str, Any]:
    return macro_payload_for_dashboard()


def enrich_open_position(pos: dict[str, Any]) -> dict[str, Any]:
    """Join open paper position with ledger/audit and chart URLs."""
    cycle_id = str(pos.get("open_cycle_id") or "") or None
    story = _trade_story_from_cycle(cycle_id)
    charts = trade_chart_urls(
        cycle_id,
        closed=False,
        ledger_chart_path=story.get("chart_path"),
        marked_chart_paths=story.get("marked_chart_paths"),
    )

    entry = float(pos.get("avg_entry") or 0)
    spot = float(pos.get("spot") or 0)
    stop = float(pos.get("stop_loss") or 0) if pos.get("stop_loss") is not None else None
    tps = _as_float_list(pos.get("take_profits") or story.get("take_profits"))
    side = str(pos.get("side") or "")
    pnl_usd = float(pos.get("unrealized_pnl_usd") or 0)
    notional = entry * float(pos.get("eth_qty") or 0)
    pnl_pct = (pnl_usd / notional * 100) if notional else 0.0

    return {
        **pos,
        "status": "open",
        "open_cycle_id": cycle_id,
        "entry": entry,
        "exit": None,
        "action": pos.get("action") or story.get("action"),
        "stop_loss": stop if stop is not None else story.get("stop_loss"),
        "take_profits": tps or story.get("take_profits") or [],
        "risk_reward": pos.get("risk_reward") if pos.get("risk_reward") is not None else story.get("risk_reward"),
        "rationale": story.get("rationale") or "",
        "setup_tags": story.get("setup_tags") or [],
        "order_block": story.get("order_block"),
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "is_winner": pnl_usd >= 0,
        "close_reason": None,
        "dist_to_sl_pct": _distance_pct(side, spot, stop) if stop else None,
        "dist_to_tp_pct": _distance_to_tp_pct(side, spot, tps),
        **charts,
    }


def enrich_closed_trade(
    trade: dict[str, Any],
    *,
    status: str = "closed",
) -> dict[str, Any]:
    """Join closed paper trade with ledger/audit and chart URLs."""
    cycle_id = str(trade.get("open_cycle_id") or "") or None
    story = _trade_story_from_cycle(cycle_id)
    charts = trade_chart_urls(
        cycle_id,
        closed=True,
        ledger_chart_path=story.get("chart_path"),
        marked_chart_paths=story.get("marked_chart_paths"),
    )
    pnl_usd = float(trade.get("realized_pnl_usd") or 0)
    pnl_pct = float(trade.get("realized_pnl_pct") or 0)
    tps = story.get("take_profits") or []

    return {
        **trade,
        "status": status,
        "action": story.get("action") or trade.get("side"),
        "stop_loss": story.get("stop_loss"),
        "take_profits": tps,
        "risk_reward": story.get("risk_reward"),
        "rationale": story.get("rationale") or "",
        "setup_tags": story.get("setup_tags") or [],
        "order_block": story.get("order_block"),
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "is_winner": pnl_usd >= 0,
        "dist_to_sl_pct": None,
        "dist_to_tp_pct": None,
        **charts,
    }


def _trade_story_from_cycle(cycle_id: str | None) -> dict[str, Any]:
    if not cycle_id:
        return {}
    row = ledger.get_suggestion_by_cycle_id(cycle_id)
    snapshot = audit.get_snapshot(cycle_id)
    suggestion = (snapshot or {}).get("suggestion") or {}
    marked = (snapshot or {}).get("marked_chart_paths") or {}

    tags_raw = (row or {}).get("setup_tags") or suggestion.get("setup_tags") or ""
    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw if t]
    else:
        tags = [t.strip() for t in str(tags_raw).split(",") if t.strip()]

    stop = None
    if row and row.get("stop_loss") is not None:
        stop = float(row["stop_loss"])
    elif suggestion.get("stop_loss") is not None:
        stop = float(suggestion["stop_loss"])

    tps = []
    if row and row.get("take_profits") is not None:
        tps = _as_float_list(row.get("take_profits"))
    elif suggestion.get("take_profits") is not None:
        tps = _as_float_list(suggestion.get("take_profits"))

    rr = None
    if row and row.get("risk_reward") is not None:
        rr = float(row["risk_reward"])
    elif suggestion.get("risk_reward") is not None:
        rr = float(suggestion["risk_reward"])

    rationale = ""
    if row and row.get("rationale"):
        rationale = str(row["rationale"])
    elif suggestion.get("rationale"):
        rationale = str(suggestion["rationale"])
    elif suggestion.get("llm_rationale"):
        rationale = str(suggestion["llm_rationale"])

    return {
        "action": (row or {}).get("action") or suggestion.get("action"),
        "chart_path": (row or {}).get("chart_path"),
        "marked_chart_paths": marked,
        "rationale": rationale,
        "setup_tags": tags,
        "stop_loss": stop,
        "take_profits": tps,
        "risk_reward": rr,
        "order_block": suggestion.get("order_block"),
        "size": (row or {}).get("size") or suggestion.get("size"),
    }


def _as_float_list(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [float(x) for x in parsed]
    return []


def _distance_pct(side: str, spot: float, level: float | None) -> float | None:
    if level is None or spot <= 0:
        return None
    return abs(spot - float(level)) / spot * 100.0


def _distance_to_tp_pct(side: str, spot: float, take_profits: list[float]) -> float | None:
    if not take_profits or spot <= 0:
        return None
    if side == "long":
        target = min(take_profits)
    else:
        target = max(take_profits)
    return abs(float(target) - spot) / spot * 100.0


def _excerpt(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "..."
