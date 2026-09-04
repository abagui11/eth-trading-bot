"""Read-only bridge to the colocated Kalshi 15m bot ledger.

The Kalshi eva_wick bot (kalshi_15m_bot repo, /opt/kalshi-15m-bot on the VPS)
keeps its paper book in its own SQLite ledger. This module mirrors the
trade_ideas_bridge pattern: fail-soft reads over ``KALSHI_DB`` so the hub
dashboard can show the bot's performance without importing its code.

Set ``KALSHI_DB=/opt/kalshi-15m-bot/ledger.db`` in the hub .env. When unset or
unreadable every payload reports ``{"available": False}`` and the tab shows a
mount hint instead of breaking.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Kalshi 15m products trade on ET walls; the bot's boss rules are ET-based.
_ET = timezone(timedelta(hours=-4))

_BOT_LABELS = {
    "control": "Control (conviction ICT)",
    "lottery": "Lottery / hail-mary",
    "adverse": "Adverse / wick-hunt",
    "eva_wick": "EVA wick (fade/overshoot)",
}

# Live sleeve: $450 moved to Kalshi shard 2 and KALSHI_BANKROLL raised.
# Soak rows stay in the ledger but must not be mixed into the tab totals —
# that is what made equity look like $66 paper + idle control.
_LIVE_EPOCH_DEFAULT = "2026-09-04T16:37:00Z"


def live_epoch() -> str:
    return (os.getenv("KALSHI_LIVE_EPOCH") or _LIVE_EPOCH_DEFAULT).strip()


def kalshi_db_path() -> Path | None:
    raw = (os.getenv("KALSHI_DB") or "").strip()
    return Path(raw) if raw else None


def enabled() -> bool:
    path = kalshi_db_path()
    return path is not None and path.exists()


def _connect() -> sqlite3.Connection | None:
    path = kalshi_db_path()
    if path is None or not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        logger.exception("Kalshi ledger unavailable at %s", path)
        return None


def _fmt_ts(value: Any) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:16]
    return dt.astimezone(_ET).strftime("%m/%d %H:%M")


def _position_row(row: sqlite3.Row) -> dict[str, Any]:
    entry = float(row["entry_cents"] or 0)
    pnl = row["pnl_usd"]
    return {
        "id": int(row["id"]),
        "bot_id": str(row["bot_id"] or "control"),
        "opened_at": _fmt_ts(row["opened_at"]),
        "closed_at": _fmt_ts(row["closed_at"]),
        "market_ticker": str(row["market_ticker"] or ""),
        "product_id": str(row["product_id"] or ""),
        "side": str(row["side"] or ""),
        "contracts": int(row["contracts"] or 0),
        "entry_cents": entry,
        "cost_usd": entry / 100.0 * int(row["contracts"] or 0),
        "result": str(row["result"] or ""),
        "pnl_usd": float(pnl) if pnl is not None else None,
        "rationale": str(row["rationale"] or ""),
    }


def performance_payload(limit: int = 15) -> dict[str, Any] | None:
    """Kalshi paper book snapshot for the hub tab; None when not mounted."""
    conn = _connect()
    if conn is None:
        return None
    try:
        states = conn.execute(
            "SELECT bot_id, starting_usd, cash_usd, realized_pnl_usd"
            " FROM paper_state ORDER BY bot_id"
        ).fetchall()
        open_rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status = 'open'"
            " ORDER BY opened_at DESC LIMIT 40"
        ).fetchall()
        epoch = live_epoch()
        closed_rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status != 'open'"
            " AND opened_at >= ? ORDER BY closed_at DESC LIMIT ?",
            (epoch, max(1, min(int(limit), 100))),
        ).fetchall()
        agg = conn.execute(
            "SELECT bot_id,"
            "  COUNT(*) AS closed,"
            "  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,"
            "  SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) AS losses,"
            "  SUM(COALESCE(pnl_usd, 0)) AS pnl_usd,"
            "  SUM(CASE WHEN result = 'flat' THEN 1 ELSE 0 END) AS early_exits"
            " FROM paper_positions"
            " WHERE status != 'open' AND opened_at >= ?"
            " GROUP BY bot_id",
            (epoch,),
        ).fetchall()
        soak_n = conn.execute(
            "SELECT COUNT(*) FROM paper_positions"
            " WHERE status != 'open' AND opened_at < ?",
            (epoch,),
        ).fetchone()[0]
    except sqlite3.Error:
        logger.exception("Kalshi ledger query failed")
        return None
    finally:
        conn.close()

    open_list = [_position_row(r) for r in open_rows]
    closed_list = [_position_row(r) for r in closed_rows]
    agg_by_bot = {str(r["bot_id"]): r for r in agg}
    open_cost_by_bot: dict[str, float] = {}
    for pos in open_list:
        open_cost_by_bot[pos["bot_id"]] = (
            open_cost_by_bot.get(pos["bot_id"], 0.0) + pos["cost_usd"]
        )

    bots: list[dict[str, Any]] = []
    for st in states:
        bot_id = str(st["bot_id"])
        a = agg_by_bot.get(bot_id)
        closed = int(a["closed"]) if a else 0
        wins = int(a["wins"] or 0) if a else 0
        losses = int(a["losses"] or 0) if a else 0
        n_open = sum(1 for p in open_list if p["bot_id"] == bot_id)
        # Idle leftover bots (the unused control book) inflate totals.
        if closed == 0 and n_open == 0 and bot_id != "eva_wick":
            continue
        decided = wins + losses
        cash = float(st["cash_usd"] or 0)
        bots.append(
            {
                "bot_id": bot_id,
                "label": _BOT_LABELS.get(bot_id, bot_id),
                "starting_usd": float(st["starting_usd"] or 0),
                "cash_usd": cash,
                "equity_usd": cash + open_cost_by_bot.get(bot_id, 0.0),
                "realized_pnl_usd": float(st["realized_pnl_usd"] or 0),
                "open": n_open,
                "closed": closed,
                "wins": wins,
                "losses": losses,
                "early_exits": int(a["early_exits"] or 0) if a else 0,
                "win_rate": (wins / decided) if decided else None,
            }
        )

    total_wins = sum(b["wins"] for b in bots)
    total_losses = sum(b["losses"] for b in bots)
    decided = total_wins + total_losses
    totals = {
        "starting_usd": sum(b["starting_usd"] for b in bots),
        "equity_usd": sum(b["equity_usd"] for b in bots),
        "realized_pnl_usd": sum(b["realized_pnl_usd"] for b in bots),
        "open": len(open_list),
        "closed": sum(b["closed"] for b in bots),
        "wins": total_wins,
        "losses": total_losses,
        "win_rate": (total_wins / decided) if decided else None,
    }
    return {
        "available": True,
        "live_epoch": epoch,
        "soak_closed": int(soak_n or 0),
        "totals": totals,
        "bots": bots,
        "open": open_list,
        "closed": closed_list,
    }
