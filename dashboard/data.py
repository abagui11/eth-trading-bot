"""Read-only data accessors for the dashboard API."""

from __future__ import annotations

import json
import time
from typing import Any

import audit
import bot_config
import ledger
import paper
import research

from dashboard.charts import h4_marked_path, latest_marked_h4_path, trade_chart_urls
from dashboard.performance import build_performance, _score_badge, score_tooltip
from dashboard.status import format_agent_status
from macro.context import macro_payload_for_dashboard

_spots_cache: tuple[dict[str, float], float] = ({}, 0.0)
_SPOT_TTL_SEC = 30.0


def get_live_spot() -> dict[str, Any]:
    """Backward-compatible single ETH spot plus multi-asset map."""
    spots = get_live_spots()
    eth = float(spots.get("spots", {}).get("ETH-USD") or spots.get("spot") or 0)
    return {
        "spot": eth,
        "eth": eth,
        "btc": float(spots.get("spots", {}).get("BTC-USD") or 0),
        "spots": spots.get("spots") or {},
        "ts": spots.get("ts"),
    }


def get_live_spots() -> dict[str, Any]:
    global _spots_cache
    now = time.time()
    if now - _spots_cache[1] > _SPOT_TTL_SEC or not _spots_cache[0]:
        prices = research.get_spot_prices()
        _spots_cache = (prices, now)
    return {"spots": dict(_spots_cache[0]), "ts": int(_spots_cache[1])}


def _latest_h4_charts(limit: int = 40) -> list[dict[str, Any]]:
    """Latest marked H4 chart URL per traded product (ETH then BTC).

    Prefers audit snapshots via the ledger; falls back to the newest marked
    H4 PNG on disk so BTC still appears when it has not yet been selected.
    """
    found: dict[str, dict[str, Any]] = {}
    traded = set(bot_config.TRADED_PRODUCTS)
    for row in ledger.get_latest(limit):
        product_id = str(row.get("product_id") or "ETH-USD")
        if product_id in found or product_id not in traded:
            continue
        cycle_id = str(row.get("cycle_id") or "")
        if not cycle_id:
            continue
        snapshot = audit.get_snapshot(cycle_id)
        if h4_marked_path((snapshot or {}).get("marked_chart_paths")) is None:
            continue
        found[product_id] = {
            "product_id": product_id,
            "product_label": bot_config.product_label(product_id),
            "cycle_id": cycle_id,
            "url": f"/api/chart/{cycle_id}",
        }
        if len(found) >= len(traded):
            break

    for product_id in bot_config.TRADED_PRODUCTS:
        if product_id in found:
            continue
        if latest_marked_h4_path(product_id) is None:
            continue
        found[product_id] = {
            "product_id": product_id,
            "product_label": bot_config.product_label(product_id),
            "cycle_id": None,
            "url": f"/api/chart/product/{product_id}/h4",
        }

    return [
        found[pid]
        for pid in bot_config.TRADED_PRODUCTS
        if pid in found
    ]


def get_status_payload() -> dict[str, Any]:
    spots_payload = get_live_spots()
    spots = spots_payload["spots"]
    eth_spot = float(spots.get("ETH-USD") or 0)
    snapshot = audit.get_latest_snapshot()
    latest_ledger = ledger.get_latest_suggestion()
    positions = paper.get_open_positions(spots=spots)
    status = format_agent_status(
        snapshot,
        ledger_row=latest_ledger,
        open_positions=positions,
    )
    verdict = None
    if status.get("cycle_id"):
        verdict = audit.get_verdict_by_cycle_id(str(status["cycle_id"]))
    h4_charts = _latest_h4_charts()
    chart_path = h4_marked_path((snapshot or {}).get("marked_chart_paths"))
    legacy_url = (
        f"/api/chart/{status['cycle_id']}"
        if chart_path and status.get("cycle_id")
        else None
    )
    breakdown = (verdict or {}).get("score_breakdown")
    score = verdict.get("score") if verdict else None
    return {
        **status,
        "spot": eth_spot,
        "spots": spots,
        "eth_spot": eth_spot,
        "btc_spot": float(spots.get("BTC-USD") or 0),
        "chart_read_score": score,
        "score_badge": _score_badge(score),
        "score_breakdown": breakdown,
        "score_tooltip": score_tooltip(score, breakdown),
        "h4_charts": h4_charts,
        "h4_chart_url": (h4_charts[0]["url"] if h4_charts else legacy_url),
        "open_by_product": _open_counts_by_product(positions),
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
        breakdown = (verdict or {}).get("score_breakdown")
        results.append(
            {
                "id": row.get("id"),
                "ts": row.get("ts"),
                "cycle_id": cycle_id,
                "action": row.get("action"),
                "product_id": row.get("product_id") or "ETH-USD",
                "price_at_suggestion": row.get("price_at_suggestion"),
                "risk_reward": row.get("risk_reward"),
                "setup_tags": row.get("setup_tags"),
                "chart_read_score": score,
                "score_badge": _score_badge(score),
                "score_breakdown": breakdown,
                "score_tooltip": score_tooltip(score, breakdown),
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
    spots = get_live_spots()["spots"]
    return [enrich_open_position(pos) for pos in paper.get_open_positions(spots=spots)]


def _participation(cycle_id: str | None) -> dict[str, Any]:
    import user_books

    if not cycle_id:
        return {
            "accepted": 0,
            "rejected": 0,
            "expired": 0,
            "pending": 0,
            "allocated_usd": 0.0,
            "total_sized_usd": 0.0,
        }
    return user_books.participation_by_cycle_id(str(cycle_id))


def get_me_payload(telegram_id: int) -> dict[str, Any] | None:
    """Personal ledger payload for /me."""
    import user_books

    spots = get_live_spots()["spots"]
    metrics = user_books.get_user_metrics(telegram_id, spots=spots)
    if not metrics.get("ok"):
        return None
    opens = []
    for pos in user_books.get_user_open_positions(telegram_id, spots=spots):
        cycle_id = str(pos.get("open_cycle_id") or "")
        charts = trade_chart_urls(cycle_id or None, closed=False)
        qty = float(pos.get("qty") or 0)
        entry = float(pos.get("avg_entry") or 0)
        pnl = float(pos.get("unrealized_pnl_usd") or 0)
        notional = entry * qty
        opens.append(
            {
                **pos,
                "status": "open",
                "product_label": bot_config.product_label(
                    str(pos.get("product_id") or "ETH-USD")
                ),
                "entry": entry,
                "exit": None,
                "pnl_usd": pnl,
                "pnl_pct": (pnl / notional * 100) if notional else 0.0,
                "is_winner": pnl >= 0,
                "size_usd": float(pos.get("suggested_size") or notional),
                "take_profits": pos.get("take_profits") or [],
                "opened_at": pos.get("opened_at"),
                "close_reason": None,
                "setup_tags": [],
                "rationale": "",
                **charts,
            }
        )
    closed_raw = user_books.get_user_closed_trades(telegram_id, limit=25)
    closed = []
    for t in closed_raw:
        qty = float(t.get("qty") or 0)
        price = float(t.get("price") or 0)
        # Approximate pnl from equity delta is not stored; show exit only.
        closed.append(
            {
                **t,
                "status": "closed",
                "product_label": bot_config.product_label(
                    str(t.get("product_id") or "ETH-USD")
                ),
                "entry": price,
                "exit": price,
                "avg_entry": price,
                "opened_at": t.get("ts"),
                "closed_at": t.get("ts"),
                "pnl_usd": 0.0,
                "pnl_pct": 0.0,
                "is_winner": True,
                "take_profits": [],
                "setup_tags": [],
                "rationale": "",
                "size_usd": None,
                "thumb_chart_url": None,
                "structure_chart_url": None,
                "execution_chart_url": None,
            }
        )
    decisions = user_books.get_user_decisions(telegram_id, limit=40)
    return {
        "telegram_id": telegram_id,
        "metrics": metrics,
        "positions": opens,
        "closed_trades": closed,
        "decisions": decisions,
    }


def get_closed_trades_payload(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    trades = paper.get_closed_trades(limit=limit + offset)
    if offset:
        trades = trades[offset : offset + limit]
    else:
        trades = trades[:limit]
    return [enrich_closed_trade(t) for t in trades]


def get_archived_trades_payload(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    trades = paper.get_archived_closed_trades(limit=limit + offset)
    if offset:
        trades = trades[offset : offset + limit]
    else:
        trades = trades[:limit]
    return [
        enrich_closed_trade(t, status="archived")
        for t in trades
    ]


def get_performance_payload() -> dict[str, Any]:
    spots = get_live_spots()["spots"]
    return build_performance(spots=spots)


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

    product_id = str(pos.get("product_id") or story.get("product_id") or "ETH-USD")
    entry = float(pos.get("avg_entry") or 0)
    spot = float(pos.get("spot") or 0)
    stop = float(pos.get("stop_loss") or 0) if pos.get("stop_loss") is not None else None
    tps = _as_float_list(pos.get("take_profits") or story.get("take_profits"))
    side = str(pos.get("side") or "")
    pnl_usd = float(pos.get("unrealized_pnl_usd") or 0)
    qty = float(pos.get("qty") or pos.get("eth_qty") or 0)
    notional = entry * qty
    size_usd = _size_usd_from_position(pos.get("suggested_size"), notional, product_id)
    pnl_pct = (pnl_usd / notional * 100) if notional else 0.0
    label = bot_config.product_label(product_id)

    return {
        **pos,
        "status": "open",
        "product_id": product_id,
        "product_label": label,
        "qty": qty,
        "eth_qty": qty,
        "size_usd": size_usd,
        "notional_usd": notional,
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
        "participation": _participation(cycle_id),
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
    product_id = str(
        trade.get("product_id") or story.get("product_id") or "ETH-USD"
    )
    qty = float(trade.get("qty") or trade.get("eth_qty") or 0)
    notional = float(trade.get("entry") or 0) * qty

    return {
        **trade,
        "status": status,
        "product_id": product_id,
        "product_label": bot_config.product_label(product_id),
        "qty": qty,
        "eth_qty": qty,
        "size_usd": notional,
        "notional_usd": notional,
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
        "participation": _participation(cycle_id),
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
        "product_id": (row or {}).get("product_id") or suggestion.get("product_id"),
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


def _open_counts_by_product(positions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pos in positions:
        pid = str(pos.get("product_id") or "ETH-USD")
        counts[pid] = counts.get(pid, 0) + 1
    return counts


def _size_usd_from_position(
    suggested_size: Any,
    notional: float,
    product_id: str,
) -> float:
    """Prefer new USD sizing; fall back to actual notional for legacy qty-sized rows."""
    if suggested_size is None:
        return notional
    size = float(suggested_size or 0)
    _, max_qty = bot_config.qty_caps(product_id)
    if 0 < size <= max_qty:
        return notional
    return size or notional


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
