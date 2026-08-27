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


def score_tooltip(
    score: int | None,
    breakdown: dict[str, Any] | None,
) -> str:
    """Explain chart-read score vs a usual ICT approach."""
    if score is None:
        return "No chart-read score for this cycle yet."
    base = (
        f"Chart-read {score}/100 — how well the agent matched a usual ICT approach "
        "(structure cited correctly, no invented levels, valid M5 OB/fib entry)."
    )
    if not breakdown:
        return base
    parts = [
        f"critical findings −{int(breakdown.get('critical') or 0)}×15",
        f"warnings −{int(breakdown.get('warning') or 0)}×5",
        f"LLM hallucinations −{int(breakdown.get('llm_hallucinations') or 0)}×20",
    ]
    if breakdown.get("sanitized"):
        parts.append("sanitized −30")
    if breakdown.get("downgraded"):
        parts.append("downgraded to no_trade")
    verified = breakdown.get("verified_claims")
    if verified is not None:
        parts.append(f"verified claims {verified}")
    return base + " Deduction detail: " + "; ".join(parts) + "."


def build_performance(
    spot: float | None = None,
    *,
    spots: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Paper PnL + chart-read quality metrics."""
    state = paper.get_state()
    positions = paper.get_open_positions(spot_price=spot, spots=spots)
    closed = paper.get_closed_trades(limit=500)

    starting = float(state.get("starting_usd") or 0)
    cash = float(state.get("cash_usd") or 0)
    equity = cash
    unrealized = 0.0
    for pos in positions:
        side = str(pos["side"])
        qty = float(pos.get("qty") or pos.get("eth_qty") or 0)
        avg_entry = float(pos["avg_entry"])
        mark = float(pos.get("spot") or avg_entry)
        if side == "long":
            equity += qty * mark
        elif side == "short":
            equity += qty * (2 * avg_entry - mark)
        unrealized += float(pos.get("unrealized_pnl_usd") or 0)

    realized = sum(float(t.get("realized_pnl_usd") or 0) for t in closed)
    win_pnls = [
        float(t.get("realized_pnl_usd") or 0)
        for t in closed
        if float(t.get("realized_pnl_usd") or 0) > 0
    ]
    loss_pnls = [
        float(t.get("realized_pnl_usd") or 0)
        for t in closed
        if float(t.get("realized_pnl_usd") or 0) < 0
    ]
    n = len(closed)
    loss_abs = abs(sum(loss_pnls))
    total_pnl = equity - starting
    win_rate = round(len(win_pnls) / n * 100, 1) if n else 0.0

    score_stats = audit.get_score_aggregates()
    open_by_product: dict[str, int] = {}
    for pos in positions:
        pid = str(pos.get("product_id") or "ETH-USD")
        open_by_product[pid] = open_by_product.get(pid, 0) + 1

    trade_scores: list[dict[str, Any]] = []
    for trade in closed[:20]:
        open_cycle = trade.get("open_cycle_id")
        verdict = (
            audit.get_verdict_by_cycle_id(str(open_cycle)) if open_cycle else None
        )
        breakdown = (verdict or {}).get("score_breakdown")
        score = verdict.get("score") if verdict else None
        trade_scores.append(
            {
                "open_cycle_id": open_cycle,
                "product_id": trade.get("product_id") or "ETH-USD",
                "realized_pnl_usd": trade.get("realized_pnl_usd"),
                "score": score,
                "score_badge": _score_badge(score),
                "score_breakdown": breakdown,
                "score_tooltip": score_tooltip(score, breakdown),
            }
        )

    total_contributed = float(
        state.get("total_contributed_usd")
        or paper.total_contributed()
        or starting
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
        "open_by_product": open_by_product,
        "closed_trade_count": n,
        "win_rate_pct": win_rate,
        "avg_pnl_usd": round(realized / n, 2) if n else None,
        "avg_win_usd": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else None,
        "avg_loss_usd": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else None,
        "profit_factor": round(sum(win_pnls) / loss_abs, 2) if loss_abs else None,
        "chart_read": score_stats,
        "recent_trade_scores": trade_scores,
        "epoch": paper.get_epoch_info(),
        "total_contributed_usd": round(total_contributed, 2),
    }
