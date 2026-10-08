"""Recorded "what did I miss?" — the autopilot counterfactual for a Kalshi lane.

Answers the tester question "if I had left autopilot on since my funds
arrived, where would I be?" by replaying the lane's recorded house entries
(`paper_positions` in the bots' own ledger, read-only) with the same sizing
rule a real autopilot Accept uses, then setting that against the user's
actual settled trades on the lane.

Honesty notes (eva-quant-evidence):

- The replay fills every house entry at the house's recorded price plus the
  published taker fee. Real autopilot re-quotes and refuses entries that
  slipped more than ``KALSHI_ACCEPT_SLIP_CENTS`` (the attempts journal shows
  such refusals happen), so this figure is the *optimistic* bound — every
  surface that shows it must call it an estimate and say fills are assumed.
- Sizing is flat at today's lane allocation
  (``alloc × POOL_KALSHI_RISK_PCT`` per window, whole contracts, capped at
  ``KALSHI_MAX_CONTRACTS_PER_ACCEPT``). A live book's budget shifts as cash
  settles and allocations change; those paths are not journaled per-window,
  so a flat base is the closest honest replay.
- The window starts at the user's first recorded deposit — the nearest
  recorded fact to "since my funds arrived". Deploy times are not journaled.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import bot_config
import kalshi_bridge
import kalshi_execute
import kalshi_gateway
import pool
import strategy_catalog

logger = logging.getLogger(__name__)


def _house_closed_trades(bot_id: str, since_iso: str) -> list[sqlite3.Row] | None:
    """Settled house entries for one bot since `since_iso`; None = no ledger."""
    db = kalshi_bridge.kalshi_db_path()
    if db is None or not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        logger.exception("counterfactual: kalshi ledger unavailable")
        return None
    try:
        return conn.execute(
            "SELECT side, result, entry_cents, opened_at FROM paper_positions "
            "WHERE bot_id = ? AND status != 'open' AND opened_at >= ? "
            "ORDER BY opened_at",
            (bot_id, since_iso),
        ).fetchall()
    except sqlite3.Error:
        logger.exception("counterfactual: house trade query failed")
        return None
    finally:
        conn.close()


def autopilot_what_if(
    telegram_id: int,
    strategy: str = strategy_catalog.KALSHI_WICK,
) -> dict[str, Any]:
    """Estimated full-autopilot book vs the user's actual settled lane book.

    Returns ``{"ok": False, "reason": ...}`` when there is nothing honest to
    compute: no deployment into the lane, no recorded deposit, or the house
    ledger is not mounted.
    """
    bot_id = kalshi_execute.STRATEGY_BOTS.get(strategy)
    if not bot_id:
        return {"ok": False, "reason": "unknown_strategy"}

    alloc = pool.get_allocation(telegram_id, strategy)
    if alloc <= 0:
        return {"ok": False, "reason": "no_allocation", "strategy": strategy}

    since_iso = pool.first_deposit_at(telegram_id)
    if not since_iso:
        return {"ok": False, "reason": "no_deposit"}

    rows = _house_closed_trades(bot_id, since_iso)
    if rows is None:
        return {"ok": False, "reason": "ledger_unavailable"}

    # Same per-window formula as pool.open_kalshi_placing, with available
    # cash assumed to cover the allocation (flat base — see module docstring).
    budget = round(alloc * float(bot_config.POOL_KALSHI_RISK_PCT), 2)
    cap = int(bot_config.KALSHI_MAX_CONTRACTS_PER_ACCEPT)

    sized = skipped = wins = losses = voids = 0
    est_pnl = 0.0
    est_fees = 0.0
    for row in rows:
        try:
            entry = float(row["entry_cents"] or 0)
        except (TypeError, ValueError):
            continue
        if not (1 <= entry <= 99):
            continue
        per_contract = (
            entry / 100.0 + kalshi_gateway.taker_fee_usd(int(round(entry)), 1)
        )
        contracts = min(int(budget // per_contract), cap)
        if contracts < 1:
            skipped += 1
            continue
        sized += 1
        # The bots' ledger stores side uppercase ("YES") and result lowercase
        # ("yes") — normalize both or every win counts as a loss.
        side = str(row["side"] or "").lower()
        result = str(row["result"] or "").lower()
        if result not in ("yes", "no"):
            # Voided market — cost refunded, exactly flat (settle_sweep rule).
            voids += 1
            continue
        fee = kalshi_gateway.taker_fee_usd(int(round(entry)), contracts)
        est_fees += fee
        if result == side:
            wins += 1
            est_pnl += contracts * (100.0 - entry) / 100.0 - fee
        else:
            losses += 1
            est_pnl += -(contracts * entry / 100.0) - fee

    actual = pool.kalshi_lane_realized(telegram_id, strategy)

    return {
        "ok": True,
        "strategy": strategy,
        "since": since_iso,
        "alloc_usd": round(alloc, 2),
        "house_trades": len(rows),
        "sized_trades": sized,
        "skipped_too_small": skipped,
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "est_pnl_usd": round(est_pnl, 2),
        "est_fees_usd": round(est_fees, 2),
        "est_return_pct": round(est_pnl / alloc * 100.0, 2),
        "actual_trades": int(actual["settled"]),
        "actual_pnl_usd": round(float(actual["pnl_usd"]), 2),
        "actual_return_pct": round(float(actual["pnl_usd"]) / alloc * 100.0, 2),
        "gap_usd": round(est_pnl - float(actual["pnl_usd"]), 2),
    }
