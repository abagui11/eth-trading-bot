"""Portfolio PnL and bot-quality aggregates for the dashboard."""

from __future__ import annotations

from typing import Any

import audit
import ledger
import paper

TRADE_ACTIONS = frozenset({"spot_buy", "spot_sell", "deriv_buy", "deriv_sell"})


def _score_badge(score: int | None) -> str:
    if score is None:
        return "none"
    if score >= 80:
        return "good"
    if score >= 60:
        return "warn"
    return "bad"


def build_performance(spot: float) -> dict[str, Any]:
    """Paper PnL + chart-read quality metrics."""
    state = paper.get_state()
    positions = paper.get_open_positions(spot)
    closed = paper.get_closed_trades(limit=500)

    starting = float(state.get("starting_usd") or 0)
    cash = float(state.get("cash_usd") or 0)
    # Mirror paper._equity (not exported)
    equity = cash
    unrealized = 0.0
    for pos in positions:
        side = str(pos["side"])
        eth_qty = float(pos["eth_qty"])
        avg_entry = float(pos["avg_entry"])
        if side == "long":
            equity += eth_qty * spot
        elif side == "short":
            equity += eth_qty * (2 * avg_entry - spot)
        unrealized += float(pos.get("unrealized_pnl_usd") or 0)

    realized = sum(float(t.get("realized_pnl_usd") or 0) for t in closed)
    total_pnl = equity - starting
    wins = sum(1 for t in closed if float(t.get("realized_pnl_usd") or 0) > 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else 0.0

    score_stats = audit.get_score_aggregates()

    trade_scores: list[dict[str, Any]] = []
    for trade in closed[:20]:
        open_cycle = trade.get("open_cycle_id")
        verdict = (
            audit.get_verdict_by_cycle_id(str(open_cycle)) if open_cycle else None
        )
        trade_scores.append(
            {
                "open_cycle_id": open_cycle,
                "realized_pnl_usd": trade.get("realized_pnl_usd"),
                "score": verdict.get("score") if verdict else None,
                "score_badge": _score_badge(verdict.get("score") if verdict else None),
            }
        )

    return {
        "starting_usd": starting,
        "cash_usd": cash,
        "equity_usd": round(equity, 2),
        "realized_pnl_usd": round(realized, 2),
        "unrealized_pnl_usd": round(unrealized, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / starting * 100, 2) if starting else 0.0,
        "open_count": len(positions),
        "closed_trade_count": len(closed),
        "win_rate_pct": win_rate,
        "chart_read": score_stats,
        "recent_trade_scores": trade_scores,
    }
