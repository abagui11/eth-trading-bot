"""Accept → real Kalshi order for the two Kalshi lanes.

The Kalshi 15m bots (external repo) mint the cards and relay them through this
bot's token with ``kalshi:accept:<strategy>:<token>`` callbacks. This module is
what an Accept reaches once the gateway is configured:

    resolve the card against the bot's ledger  →  re-quote the market  →
    refuse stale/slipped setups  →  reserve the user's budget (pool)  →
    place a limit buy on the house Kalshi account  →  book the actual fill.

Everything that moves money goes through `pool`'s journaled events; this file
owns venue choreography only. The card is resolved from ``KALSHI_DB`` (the
bots' own ledger, read-only) rather than trusted from callback payload — the
callback names a position, the ledger says what it actually was.

Sizing note (eva-quant-evidence): POOL_KALSHI_RISK_PCT is a *cost cap per
window*, not a tuned parameter — a binary contract's cost is its entire risk,
and this fraction of the lane allocation bounds it. No recorded-book claim is
made for the cap's specific value.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import bot_config
import kalshi_bridge
import kalshi_gateway
import pool
import strategy_catalog

logger = logging.getLogger(__name__)

# Which external bot's entries each hub lane mirrors.
STRATEGY_BOTS = {
    strategy_catalog.KALSHI_REVERSAL: "eva_streak",
    strategy_catalog.KALSHI_WICK: "eva_wick",
}

# How long to wait for an IOC-style fill before cancelling the remainder.
_FILL_POLL_TRIES = 4
_FILL_POLL_SLEEP_SEC = 0.8


def enabled() -> bool:
    """Real Kalshi accepts need both the gateway keys and the bots' ledger."""
    return kalshi_gateway.configured() and kalshi_bridge.enabled()


def _age_minutes(opened_at: str) -> float | None:
    try:
        dt = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def resolve_card(strategy: str, token: str) -> dict[str, Any]:
    """What trade does this card refer to, per the bot's own ledger?

    A numeric token is a ``paper_positions.id``; anything else falls back to
    the bot's most recent open entry. Either way the row must belong to the
    lane's bot and be fresh enough to still be the same trade — the windows
    settle every 15 minutes, so an old card names a market that is already
    decided.
    """
    bot_id = STRATEGY_BOTS.get(strategy)
    if not bot_id:
        return {"ok": False, "reason": "unknown_strategy"}
    db = kalshi_bridge.kalshi_db_path()
    if db is None or not db.exists():
        return {"ok": False, "reason": "ledger_unavailable"}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {"ok": False, "reason": "ledger_unavailable"}
    try:
        row = None
        if str(token).isdigit():
            row = conn.execute(
                "SELECT * FROM paper_positions WHERE id = ? AND bot_id = ?",
                (int(token), bot_id),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM paper_positions WHERE bot_id = ? AND "
                "status = 'open' ORDER BY opened_at DESC LIMIT 1",
                (bot_id,),
            ).fetchone()
    except sqlite3.Error:
        logger.exception("kalshi resolve: ledger query failed")
        return {"ok": False, "reason": "ledger_unavailable"}
    finally:
        conn.close()

    if row is None:
        return {"ok": False, "reason": "no_open_entry"}
    age = _age_minutes(str(row["opened_at"]))
    max_age = float(bot_config.KALSHI_ACCEPT_MAX_AGE_MIN)
    if age is None or age > max_age:
        return {"ok": False, "reason": "stale_card",
                "age_min": round(age, 1) if age is not None else None,
                "max_age_min": max_age}
    side = str(row["side"] or "").lower()
    if side not in ("yes", "no"):
        return {"ok": False, "reason": "bad_ledger_side", "side": side}
    return {
        "ok": True,
        "market_ticker": str(row["market_ticker"] or ""),
        "side": side,
        "entry_cents": float(row["entry_cents"] or 0),
        "position_id": int(row["id"]),
    }


def accept(telegram_id: int, strategy: str, token: str) -> dict[str, Any]:
    """One user's Accept on one Kalshi card, end to end.

    Money-safety ordering: the budget is reserved *before* the order exists,
    and the reserve is only released by what the venue reports back. An
    ambiguous venue state (we cannot read the fills) leaves the row in
    'placing' with the reserve held — the stale-placing sweep pages an admin
    rather than anyone guessing.
    """
    if not enabled():
        return {"ok": False, "reason": "not_enabled"}

    card = resolve_card(strategy, token)
    if not card.get("ok"):
        return card

    ticker = card["market_ticker"]
    side = card["side"]

    # Re-quote at the moment of the tap — the Kalshi revalidation gate.
    try:
        market = kalshi_gateway.get_market(ticker)
    except kalshi_gateway.KalshiError as exc:
        logger.warning("kalshi accept: quote failed for %s: %s", ticker, exc)
        return {"ok": False, "reason": "quote_failed"}
    if str(market.get("status") or "").lower() not in ("open", "active", ""):
        return {"ok": False, "reason": "market_closed",
                "status": market.get("status")}
    ask = kalshi_gateway.ask_cents(market, side)
    if ask is None:
        return {"ok": False, "reason": "no_quote"}
    slip = ask - float(card["entry_cents"])
    if slip > float(bot_config.KALSHI_ACCEPT_SLIP_CENTS):
        return {"ok": False, "reason": "slipped",
                "entry_cents": card["entry_cents"], "ask_cents": ask,
                "max_slip_cents": float(bot_config.KALSHI_ACCEPT_SLIP_CENTS)}

    ref = f"k_{strategy}:{card['position_id']}:{telegram_id}"
    fee_per_contract = kalshi_gateway.taker_fee_usd(ask, 1)
    reserved = pool.open_kalshi_placing(
        ref, telegram_id, strategy,
        market_ticker=ticker, side=side, limit_cents=ask,
        fee_per_contract_usd=fee_per_contract,
    )
    if not reserved.get("ok"):
        return reserved
    contracts = int(reserved["contracts"])

    try:
        order = kalshi_gateway.place_limit_buy(
            ticker, side, contracts, ask,
            client_order_id=f"eva-{uuid.uuid4().hex[:20]}",
        )
    except kalshi_gateway.KalshiError as exc:
        # The order was refused outright — nothing exists at the venue, so
        # the reserve goes straight back.
        logger.warning("kalshi accept: order refused on %s: %s", ticker, exc)
        pool.finish_kalshi_open(
            ref, filled_contracts=0, entry_cents=0, cost_usd=0, fee_usd=0,
            order_id=None,
        )
        return {"ok": False, "reason": "order_refused"}

    order_id = str(order["order_id"])
    try:
        for _ in range(_FILL_POLL_TRIES):
            state = kalshi_gateway.get_order(order_id)
            if int(state.get("remaining_count") or 0) <= 0:
                break
            if str(state.get("status") or "").lower() in ("canceled", "cancelled"):
                break
            time.sleep(_FILL_POLL_SLEEP_SEC)
        else:
            kalshi_gateway.cancel_order(order_id)
        summary = kalshi_gateway.fill_summary(order_id, side)
    except kalshi_gateway.KalshiError as exc:
        # Ambiguous: an order exists and we cannot read what it did. The
        # reserve stays held and the 'placing' row is the evidence a human
        # follows up on. Releasing here could free money that is actually in
        # contracts.
        logger.error("kalshi accept: fill state unknown for %s: %s", order_id, exc)
        return {"ok": False, "reason": "fill_unknown", "order_id": order_id}

    booked = pool.finish_kalshi_open(
        ref,
        filled_contracts=int(summary["contracts"]),
        entry_cents=float(summary["avg_cents"]),
        cost_usd=float(summary["cost_usd"]),
        fee_usd=float(summary["fee_usd"]),
        order_id=order_id,
    )
    if not booked.get("ok"):
        logger.error("kalshi accept: booking failed for %s: %s", ref, booked)
        return {"ok": False, "reason": "booking_failed", "order_id": order_id}

    if int(summary["contracts"]) <= 0:
        return {"ok": False, "reason": "unfilled", "ask_cents": ask}
    return {
        "ok": True,
        "market_ticker": ticker,
        "side": side,
        "contracts": int(summary["contracts"]),
        "avg_cents": float(summary["avg_cents"]),
        "cost_usd": float(summary["cost_usd"]),
        "fee_usd": float(summary["fee_usd"]),
        "order_id": order_id,
    }


def settle_sweep() -> list[dict[str, Any]]:
    """Book settlements for every open user position; returns DM payloads.

    Reads each market once per sweep. A win pays $1 a contract; a loss pays
    zero; anything that is neither yes nor no (voided market) refunds the
    cost so the user is exactly flat. Booking is idempotent per position via
    the journal refs, so a crash mid-sweep re-runs safely.
    """
    if not kalshi_gateway.configured():
        return []
    results: list[dict[str, Any]] = []
    markets: dict[str, dict[str, Any]] = {}
    for row in pool.open_kalshi_rows("open"):
        ticker = str(row["market_ticker"])
        market = markets.get(ticker)
        if market is None:
            try:
                market = kalshi_gateway.get_market(ticker)
            except kalshi_gateway.KalshiError:
                logger.warning("kalshi settle: cannot read %s — skipped", ticker)
                continue
            markets[ticker] = market
        status = str(market.get("status") or "").lower()
        if status not in ("settled", "finalized"):
            continue
        result = str(market.get("result") or "").lower()
        contracts = int(row["contracts"])
        cost = float(row["cost_usd"])
        if result == str(row["side"]):
            payout = contracts * 1.0
        elif result in ("yes", "no"):
            payout = 0.0
        else:  # voided / scratched market: refund, exactly flat
            payout = cost
            result = result or "void"
        settled = pool.settle_kalshi_position(
            int(row["id"]), result=result, payout_usd=payout,
        )
        if settled.get("ok"):
            results.append(settled)
    return results
