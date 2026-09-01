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

from dashboard.charts import (
    h4_marked_path,
    latest_marked_h4_path,
    resolve_chart_path,
    trade_chart_urls,
)
from dashboard.performance import build_performance, _score_badge, score_tooltip
from dashboard.status import format_agent_status
from macro.context import macro_payload_for_dashboard

_spots_cache: tuple[dict[str, float], float] = ({}, 0.0)
_SPOT_TTL_SEC = 30.0


def reset_spot_cache() -> None:
    """Drop the memoized spot quotes so the next read refetches."""
    global _spots_cache
    _spots_cache = ({}, 0.0)


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
        "watchdog_enabled": bot_config.WATCHDOG_ENABLED,
        "watchdog_execute_enabled": bot_config.watchdog_execute_enabled(),
        "watchdog_allow_shorts": bot_config.WATCHDOG_ALLOW_SHORTS,
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


def get_archived_performance_payload() -> dict[str, Any]:
    return paper.get_archived_book_summary()


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

    effective_stop = stop if stop is not None else story.get("stop_loss")
    effective_tps = tps or story.get("take_profits") or []
    tps_progress = build_tp_progress(
        side, entry, effective_tps, tps_hit=int(pos.get("tps_hit") or 0)
    )
    stop_state = build_stop_state(
        side, entry, effective_stop, story.get("stop_loss"), qty_open=qty
    )

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
        "stop_loss": effective_stop,
        "initial_stop_loss": story.get("stop_loss"),
        "stop_state": stop_state,
        "take_profits": effective_tps,
        "tp_progress": tps_progress,
        "tps_hit": int(pos.get("tps_hit") or 0),
        "tp_count": len(tps_progress),
        "risk_reward": pos.get("risk_reward") if pos.get("risk_reward") is not None else story.get("risk_reward"),
        "risk_reward_kind": "planned",
        "rationale": story.get("rationale") or "",
        "setup_tags": story.get("setup_tags") or [],
        "order_block": story.get("order_block"),
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "unrealized_pnl_pct": pnl_pct,
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

    planned_rr = story.get("risk_reward")
    realized_rr = realized_r_multiple(
        pnl_usd, qty, float(trade.get("entry") or 0), story.get("stop_loss")
    )

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
        "risk_reward": realized_rr if realized_rr is not None else planned_rr,
        "risk_reward_kind": "realized" if realized_rr is not None else "planned",
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


def enrich_live_trades(
    trades: list[dict[str, Any]], *, closed: bool = False
) -> list[dict[str, Any]]:
    """Shape live ledger rows for the shared ``trade_card`` macro.

    The live book was the only book rendered as a bare table: no thesis, no
    charts, and no mark — so an open position never showed whether it was up.
    Paper positions arrive with ``spot`` and unrealized pnl already attached by
    the paper engine; live rows carry neither, so both are derived here.
    """
    spots = get_live_spots().get("spots") or {}
    enriched: list[dict[str, Any]] = []
    for trade in trades:
        row = dict(trade)
        cycle_id = str(row.get("cycle_id") or "") or None
        try:
            story = _trade_story_from_cycle(cycle_id) if cycle_id else {}
        except Exception:
            story = {}
        charts = trade_chart_urls(
            cycle_id,
            closed=closed,
            ledger_chart_path=story.get("chart_path"),
            marked_chart_paths=story.get("marked_chart_paths"),
        )

        product_id = str(row.get("product_id") or story.get("product_id") or "ETH-USD")
        side = str(row.get("side") or "")
        qty = float(row.get("qty") or 0)
        # Scale-outs leave part of the clip on the exchange; only that part is
        # still marked to market. Pre-migration rows have no qty_open, and 0 is
        # a real value, so None is the only fallback signal.
        raw_open = row.get("qty_open")
        qty_open = qty if raw_open is None else float(raw_open)
        banked = float(row.get("realized_pnl_usd") or 0.0)
        entry = float(row.get("entry") or 0)
        notional = entry * qty
        stop = row.get("stop_loss")
        stop = float(stop) if stop is not None else story.get("stop_loss")
        # The executor overwrites stop_loss on every trail. Rows opened since
        # the column was added carry the level actually armed; older ones fall
        # back to the originating suggestion, which is only the *planned* stop
        # (revalidation can re-anchor it at fill time) but is better than none.
        raw_initial = row.get("initial_stop_loss")
        tps = _as_float_list(row.get("take_profits_json")) or _as_float_list(
            story.get("take_profits")
        )
        legs = exit_legs(row.get("exit_fills_json"))
        # initial_stop_loss was backfilled from stop_loss for rows opened
        # before the column existed. If that copy landed after a trail, the
        # "opening" stop is already at TP1 and the card thinks nothing moved.
        initial_stop = recover_opening_stop(
            side,
            entry,
            float(raw_initial) if raw_initial is not None else None,
            story.get("stop_loss"),
        )

        if closed:
            exit_price = float(row.get("exit_price") or 0) or None
            pnl_usd = float(row.get("pnl_usd") or 0)
            unrealized = 0.0
            mark = exit_price
        else:
            exit_price = None
            mark = float(spots.get(product_id) or 0) or None
            direction = 1.0 if side == "long" else -1.0
            unrealized = (mark - entry) * qty_open * direction if mark else 0.0
            # Headline is the trade's result so far: what's banked plus what's
            # still riding.
            pnl_usd = unrealized + banked

        # Auto vs manual is the thing an operator most wants at a glance, so it
        # leads the badges ahead of the cycle's own setup tags.
        tags = [str(row.get("fill_type") or "auto")]
        tags += [t for t in (story.get("setup_tags") or []) if t not in tags]

        tps_progress = build_tp_progress(side, entry, tps, legs=legs)
        stop_state = build_stop_state(
            side, entry, stop, initial_stop, qty_open=qty_open
        )
        open_notional = entry * qty_open

        planned_rr = story.get("risk_reward")
        realized_rr = (
            realized_r_multiple(pnl_usd, qty, entry, initial_stop) if closed else None
        )

        enriched.append(
            {
                **row,
                "status": "closed" if closed else "open",
                "product_id": product_id,
                "product_label": bot_config.product_label(product_id),
                "open_cycle_id": cycle_id,
                "qty": qty,
                "qty_open": qty_open,
                "realized_pnl_usd": banked,
                "unrealized_pnl_usd": unrealized,
                "scaled_out": (not closed) and qty_open < qty - 1e-9,
                "entry": entry,
                "exit": exit_price,
                "spot": mark,
                "notional_usd": notional,
                "size_usd": notional,
                "action": story.get("action"),
                "stop_loss": stop,
                "initial_stop_loss": initial_stop,
                "stop_state": stop_state,
                "take_profits": tps,
                "tp_progress": tps_progress,
                "tps_hit": sum(1 for r in tps_progress if r["hit"]),
                "tp_count": len(tps_progress),
                "exit_legs": legs,
                "risk_reward": realized_rr if realized_rr is not None else planned_rr,
                "risk_reward_kind": (
                    "realized" if realized_rr is not None else "planned"
                ),
                "rationale": story.get("rationale") or "",
                "setup_tags": tags,
                "order_block": story.get("order_block"),
                "pnl_usd": pnl_usd,
                "pnl_pct": (pnl_usd / notional * 100) if notional else 0.0,
                "unrealized_pnl_pct": (
                    (unrealized / open_notional * 100) if open_notional else 0.0
                ),
                "realized_pnl_pct": (banked / notional * 100) if notional else 0.0,
                "is_winner": pnl_usd >= 0,
                "dist_to_sl_pct": (
                    _distance_pct(side, mark or 0, stop) if stop and not closed else None
                ),
                "dist_to_tp_pct": (
                    _distance_to_tp_pct(side, mark or 0, tps) if not closed else None
                ),
                "participation": _participation(cycle_id),
                "case_study_url": (
                    f"/api/live-chart/{int(row['id'])}"
                    if closed
                    and str(row.get("source") or "") == "hq"
                    and resolve_chart_path(row.get("case_study_path")) is not None
                    else None
                ),
                **charts,
            }
        )
    return enriched


def live_unrealized_usd(open_rows: list[dict[str, Any]]) -> float:
    """Mark-to-market total for the open live book (already enriched).

    Banked scale-outs are excluded — those are realized and already counted by
    the live performance read.
    """
    return sum(float(r.get("unrealized_pnl_usd") or 0.0) for r in open_rows)


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


def _tp_price_reached(side: str, price: float, level: float) -> bool:
    return price >= level if side == "long" else price <= level


def realized_r_multiple(
    pnl_usd: float,
    qty: float,
    entry: float,
    opening_stop: float | None,
) -> float | None:
    """How many R the trade actually banked: P&L over dollars originally at risk.

    Planned R:R is entry→TP1 and describes a different trade than the ladder
    executes. Closed cards should report this instead.
    """
    if opening_stop is None or qty <= 0 or entry <= 0:
        return None
    risk = abs(float(entry) - float(opening_stop)) * float(qty)
    if risk <= 0:
        return None
    return round(float(pnl_usd) / risk, 2)


def recover_opening_stop(
    side: str,
    entry: float,
    ledger_initial: float | None,
    planned: float | None,
) -> float | None:
    """Prefer the planned stop when the ledger 'initial' is already a trail.

    ``initial_stop_loss`` was backfilled from ``stop_loss`` for rows that
    opened before the column existed. If that copy happened after a trail,
    a long's opening stop sits *above* entry and the UI hides the SL pip.
    """
    if ledger_initial is None:
        return float(planned) if planned is not None else None
    initial = float(ledger_initial)
    if not entry:
        return initial
    already_trailed = (side == "long" and initial > entry) or (
        side == "short" and initial < entry
    )
    if already_trailed:
        return float(planned) if planned is not None else float(entry)
    return initial


def ordered_take_profits(
    side: str, take_profits: list[float], entry: float
) -> list[float]:
    """Targets in the order price would reach them (TP1 nearest).

    Mirrors ``execute._ordered_tps`` so the numbering a reader sees on the
    dashboard is the same numbering the executor banked against. Levels on the
    wrong side of entry are dropped; if that leaves nothing the raw list is
    sorted anyway rather than showing a trade with no targets.
    """
    levels = [float(tp) for tp in (take_profits or []) if tp]
    if not levels:
        return []
    if side == "long":
        ahead = sorted(tp for tp in levels if tp > entry)
        return ahead or sorted(levels)
    ahead = sorted((tp for tp in levels if tp < entry), reverse=True)
    return ahead or sorted(levels, reverse=True)


def exit_legs(raw: Any) -> list[dict[str, Any]]:
    """Booked exit fills oldest-first, from ``live_trades.exit_fills_json``."""
    if isinstance(raw, dict):
        booked = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            booked = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        return []
    if not isinstance(booked, dict):
        return []
    legs = [
        {
            "order_id": key,
            "qty": float(leg.get("qty") or 0),
            "price": float(leg.get("price") or 0),
            "pnl_usd": float(leg.get("pnl_usd") or 0),
            "reason": str(leg.get("reason") or ""),
            "at": str(leg.get("at") or ""),
        }
        for key, leg in booked.items()
        if isinstance(leg, dict)
    ]
    legs.sort(key=lambda leg: leg["at"])
    return legs


def build_tp_progress(
    side: str,
    entry: float,
    take_profits: list[float],
    *,
    legs: list[dict[str, Any]] | None = None,
    tps_hit: int | None = None,
) -> list[dict[str, Any]]:
    """Per-target ladder state: which rungs paid out, and for how much.

    The live book has no tp1/tp2/tp3 columns — a target hit is a booked exit
    leg — so which rungs paid is inferred from fill *prices*, not from how
    many legs were tagged ``take_profit``. A profitable stop (trailed to TP1)
    used to light TP3 because it was the third green fill. Paper positions
    carry a plain ``tps_hit`` counter instead and have no per-leg P&L, so
    those rungs show as hit with no amount.
    """
    ladder = ordered_take_profits(side, take_profits, entry)
    if not ladder:
        return []
    direction = 1.0 if side == "long" else -1.0
    assignments: list[dict[str, Any] | None] = [None] * len(ladder)
    if legs is None:
        hit_flags = [idx < int(tps_hit or 0) for idx in range(len(ladder))]
    else:
        prices: list[float] = []
        for leg in legs:
            if str(leg.get("reason") or "") == "stop_loss":
                continue
            try:
                px = float(leg.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if px > 0:
                prices.append(px)
        if prices:
            still_reaching = True
            hit_flags = []
            for level in ladder:
                reached = still_reaching and any(
                    _tp_price_reached(side, px, level) for px in prices
                )
                if not reached:
                    still_reaching = False
                hit_flags.append(reached)
            for leg in legs:
                if str(leg.get("reason") or "") == "stop_loss":
                    continue
                try:
                    px = float(leg.get("price") or 0)
                except (TypeError, ValueError):
                    continue
                if px <= 0:
                    continue
                best: int | None = None
                for idx, level in enumerate(ladder):
                    if assignments[idx] is not None:
                        continue
                    if _tp_price_reached(side, px, level):
                        best = idx
                    else:
                        break
                if best is not None:
                    assignments[best] = leg
        else:
            tp_legs = [leg for leg in legs if leg.get("reason") == "take_profit"]
            hit_flags = [idx < len(tp_legs) for idx in range(len(ladder))]
            for idx, leg in enumerate(tp_legs):
                if idx < len(assignments):
                    assignments[idx] = leg

    rungs: list[dict[str, Any]] = []
    for idx, price in enumerate(ladder):
        leg = assignments[idx]
        rungs.append(
            {
                "label": f"TP{idx + 1}",
                "price": round(float(price), 2),
                "hit": hit_flags[idx],
                "pnl_usd": round(leg["pnl_usd"], 2) if leg else None,
                "qty": leg["qty"] if leg else None,
                "at": leg["at"] if leg else None,
                "pct_from_entry": (
                    round((float(price) - entry) / entry * 100.0 * direction, 2)
                    if entry
                    else None
                ),
            }
        )
    return rungs


def build_stop_state(
    side: str,
    entry: float,
    current_stop: float | None,
    initial_stop: float | None,
    *,
    qty_open: float = 0.0,
) -> dict[str, Any]:
    """Where the stop sits now versus where the thesis first put it.

    ``live_trades.stop_loss`` is overwritten in place every time the executor
    trails, so the original level only survives on the ledger suggestion the
    trade was opened from. Without that comparison a trailed stop is
    indistinguishable from the trade's opening risk.
    """
    if current_stop is None:
        return {
            "current": None,
            "initial": None,
            "trailed": False,
            "at_breakeven": False,
            "locked_pnl_usd": None,
            "label": None,
        }
    current = float(current_stop)
    initial = float(initial_stop) if initial_stop is not None else None
    direction = 1.0 if side == "long" else -1.0
    trailed = initial is not None and abs(current - initial) > 0.005
    # "Beyond entry in the favourable direction" — for a short that means the
    # stop moved *down*, so the raw comparison has to be sign-corrected.
    beyond_entry = (current - entry) * direction
    at_breakeven = beyond_entry >= -0.005

    if not trailed:
        label = "initial"
    elif abs(beyond_entry) <= 0.005:
        label = "breakeven"
    elif at_breakeven:
        label = "profit locked"
    else:
        label = "trailed"

    return {
        "current": round(current, 2),
        "initial": round(initial, 2) if initial is not None else None,
        "trailed": trailed,
        "at_breakeven": at_breakeven,
        "locked_pnl_usd": (
            round(beyond_entry * float(qty_open or 0), 2)
            if at_breakeven and qty_open
            else None
        ),
        "label": label,
    }


def _excerpt(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "..."
