"""Payload for the private investor view (/investors).

Everything an outside reader needs to judge the book in a few seconds:
portfolio value, realized gain for the day and the year, unrealized, collateral
health, and every open position with its ladder state. The hub's Eva tab is
built for an operator and assumes context this reader does not have, so the
numbers are re-aggregated here rather than reused piecemeal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import bot_config
import live_ledger
import paper

from dashboard import data
from dashboard.account import CONFIGURED_CAPITAL_USD, build_health

_SOURCE = "hq"
SLEEVE_LABELS = {"hq": "Eva"}
SLEEVE_CAPITAL = {"hq": float(bot_config.LIVE_HQ_EQUITY_USD)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mark_notional(trade: dict[str, Any]) -> float:
    """Current exposure of one open clip.

    Marked to the live price, not entry: what matters for collateral health is
    what the position is worth now. Falls back to entry when the spot read is
    missing so a stale quote can't silently zero out exposure.
    """
    qty_open = float(trade.get("qty_open") or 0.0)
    if qty_open <= 0:
        return 0.0
    price = float(trade.get("spot") or 0.0) or float(trade.get("entry") or 0.0)
    return abs(price * qty_open)


def _exposure(open_trades: list[dict[str, Any]]) -> dict[str, Any]:
    by_sleeve: dict[str, dict[str, Any]] = {}
    gross = 0.0
    net = 0.0
    for trade in open_trades:
        sleeve = str(trade.get("source") or "hq")
        notional = _mark_notional(trade)
        gross += notional
        net += notional * (1.0 if str(trade.get("side")) == "long" else -1.0)
        bucket = by_sleeve.setdefault(
            sleeve,
            {
                "sleeve": sleeve,
                "label": SLEEVE_LABELS.get(sleeve, sleeve.upper()),
                "notional_usd": 0.0,
                "open": 0,
                "unrealized_usd": 0.0,
                "capital_usd": SLEEVE_CAPITAL.get(sleeve),
            },
        )
        bucket["notional_usd"] += notional
        bucket["open"] += 1
        bucket["unrealized_usd"] += float(trade.get("unrealized_pnl_usd") or 0.0)

    for bucket in by_sleeve.values():
        bucket["notional_usd"] = round(bucket["notional_usd"], 2)
        bucket["unrealized_usd"] = round(bucket["unrealized_usd"], 2)

    return {
        "gross_notional_usd": round(gross, 2),
        "net_notional_usd": round(net, 2),
        "open_count": len(open_trades),
        "by_sleeve": [by_sleeve[k] for k in ("hq",) if k in by_sleeve],
    }


def _daily_stats(days: list[dict[str, Any]], today: str) -> dict[str, Any]:
    traded = [d for d in days if d["realized_pnl_usd"] != 0 or d["exits_n"]]
    green = [d for d in traded if d["realized_pnl_usd"] > 0]
    red = [d for d in traded if d["realized_pnl_usd"] < 0]
    today_row = next((d for d in days if d["date"] == today), None)
    return {
        "today_usd": round(float((today_row or {}).get("realized_pnl_usd") or 0.0), 2),
        "today_closed": int((today_row or {}).get("closed_n") or 0),
        "today_exits": int((today_row or {}).get("exits_n") or 0),
        "ytd_usd": round(sum(d["realized_pnl_usd"] for d in days), 2),
        "trading_days": len(traded),
        "green_days": len(green),
        "red_days": len(red),
        "best_day": max(traded, key=lambda d: d["realized_pnl_usd"], default=None),
        "worst_day": min(traded, key=lambda d: d["realized_pnl_usd"], default=None),
        "avg_day_usd": (
            round(sum(d["realized_pnl_usd"] for d in traded) / len(traded), 2)
            if traded
            else None
        ),
    }


def _capital_base() -> dict[str, Any]:
    """Eva's allocated sleeve — mill capital is not mixed in."""
    return {"usd": round(CONFIGURED_CAPITAL_USD, 2), "basis": "configured"}


def _pct(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator * 100.0, 2)


def _parse_liq_price(pos: dict[str, Any]) -> float | None:
    """Coinbase quotes $0 when they are not estimating a liquidation price."""
    raw = pos.get("raw") or pos
    for key in ("liquidation_price", "estimated_liquidation_price"):
        node = raw.get(key)
        if isinstance(node, dict):
            node = node.get("value")
        if node in (None, ""):
            continue
        try:
            px = float(node)
        except (TypeError, ValueError):
            continue
        if px > 0:
            return px
    return None


def _attach_liquidation(trades: list[dict[str, Any]]) -> None:
    """Best-effort per-instrument liq price. Always sets the key (None → n/a)."""
    instruments = {str(t.get("instrument") or "") for t in trades}
    instruments.discard("")
    found: dict[str, float | None] = {}
    if instruments:
        try:
            from coinbase_deriv import get_gateway

            gw = get_gateway()
            for inst in instruments:
                try:
                    found[inst] = _parse_liq_price(gw.get_position(inst))
                except Exception:
                    found[inst] = None
        except Exception:
            pass
    for trade in trades:
        inst = str(trade.get("instrument") or "")
        trade["liquidation_price"] = found.get(inst)


def build_investor_payload(
    *,
    closed_limit: int = 40,
    paper_limit: int = 15,
    include_paper: bool = True,
) -> dict[str, Any]:
    """Full investor snapshot. Blocking — call from a sync route handler.

    ``include_paper`` is off for the polling endpoint: the paper books re-read
    the ledger and audit tables per trade, and none of it changes between
    refreshes.
    """
    # HQ only — mill clips share the Coinbase account but are a different
    # product, and mixing them here made Eva's book unreadable.
    open_trades = data.enrich_live_trades(live_ledger.get_open_trades(source=_SOURCE))
    _attach_liquidation(open_trades)
    closed_trades = data.enrich_live_trades(
        live_ledger.get_closed_trades(limit=closed_limit, source=_SOURCE),
        closed=True,
    )
    performance = live_ledger.get_live_performance()
    hq_perf = (performance.get("by_source") or {}).get(_SOURCE) or {}

    now = datetime.now(timezone.utc)
    year = now.year
    today = now.strftime("%Y-%m-%d")
    days = live_ledger.get_realized_by_day(source=_SOURCE, year=year)
    daily = _daily_stats(days, today)

    unrealized = round(data.live_unrealized_usd(open_trades), 2)
    realized_total = float(hq_perf.get("pnl_usd") or 0.0)
    realized_ytd = float(daily["ytd_usd"])

    exposure = _exposure(open_trades)
    base = _capital_base()
    base_usd = float(base["usd"])
    # Eva NAV from the allocated sleeve, not Coinbase account equity.
    portfolio_value = round(base_usd + realized_total + unrealized, 2)

    health = build_health(
        equity_usd=portfolio_value,
        gross_notional_usd=exposure["gross_notional_usd"],
    )

    paper_books: dict[str, Any] = {"available": False}
    if include_paper:
        paper_books = {
            "available": True,
            "current": data.get_performance_payload(),
            "current_label": bot_config.PAPER_EPOCH_LABEL,
            "current_trades": data.get_closed_trades_payload(limit=paper_limit),
            "current_open": data.get_open_positions_payload(),
            "archived": data.get_archived_performance_payload(),
            "archived_trades": data.get_archived_trades_payload(limit=paper_limit),
            "epoch": paper.get_epoch_info(),
        }

    return {
        "generated_at": _now_iso(),
        "year": year,
        "health": health,
        "exposure": exposure,
        "portfolio": {
            "value_usd": round(portfolio_value, 2),
            "value_source": "eva_sleeve",
            "capital_base_usd": base_usd,
            "capital_base_basis": base["basis"],
            "unrealized_usd": unrealized,
            "unrealized_pct": _pct(unrealized, base_usd),
            "realized_today_usd": daily["today_usd"],
            "realized_today_pct": _pct(daily["today_usd"], base_usd),
            "realized_ytd_usd": realized_ytd,
            "realized_ytd_pct": _pct(realized_ytd, base_usd),
            "realized_all_time_usd": round(realized_total, 2),
            "total_pnl_usd": round(realized_ytd + unrealized, 2),
            "total_pnl_pct": _pct(realized_ytd + unrealized, base_usd),
        },
        "daily": {**daily, "days": days},
        "performance": performance,
        "open_trades": open_trades,
        "closed_trades": closed_trades,
        "paper": paper_books,
    }
