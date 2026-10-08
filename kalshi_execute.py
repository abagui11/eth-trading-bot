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
        "age_min": round(age, 2),
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
    # Context carried on every result from here down, success or refusal —
    # this is what the attempt journal measures the slip gate from.
    age_min = card.get("age_min")
    base = {
        "market_ticker": ticker,
        "side": side,
        "entry_cents": float(card["entry_cents"]),
        "position_id": int(card["position_id"]),
        "lag_sec": (
            round(float(age_min) * 60.0, 1) if age_min is not None else None
        ),
    }

    # Re-quote at the moment of the tap — the Kalshi revalidation gate.
    try:
        market = kalshi_gateway.get_market(ticker)
    except kalshi_gateway.KalshiError as exc:
        logger.warning("kalshi accept: quote failed for %s: %s", ticker, exc)
        return {**base, "ok": False, "reason": "quote_failed"}
    if str(market.get("status") or "").lower() not in ("open", "active", ""):
        return {**base, "ok": False, "reason": "market_closed",
                "status": market.get("status")}
    ask = kalshi_gateway.ask_cents(market, side)
    if ask is None:
        return {**base, "ok": False, "reason": "no_quote"}
    base["ask_cents"] = float(ask)
    slip = ask - float(card["entry_cents"])
    if slip > float(bot_config.KALSHI_ACCEPT_SLIP_CENTS):
        logger.info(
            "kalshi accept gate: slipped %s %s — house %.1f¢, ask %.0f¢ "
            "(gap %.1f¢ > %s¢), lag %ss",
            ticker, side, float(card["entry_cents"]), ask, slip,
            bot_config.KALSHI_ACCEPT_SLIP_CENTS, base["lag_sec"],
        )
        return {**base, "ok": False, "reason": "slipped",
                "max_slip_cents": float(bot_config.KALSHI_ACCEPT_SLIP_CENTS)}

    ref = f"k_{strategy}:{card['position_id']}:{telegram_id}"
    fee_per_contract = kalshi_gateway.taker_fee_usd(ask, 1)
    reserved = pool.open_kalshi_placing(
        ref, telegram_id, strategy,
        market_ticker=ticker, side=side, limit_cents=ask,
        fee_per_contract_usd=fee_per_contract,
    )
    if not reserved.get("ok"):
        return {**base, **reserved}
    contracts = int(reserved["contracts"])
    client_order_id = f"eva-{uuid.uuid4().hex[:20]}"

    try:
        order = kalshi_gateway.place_limit_buy(
            ticker, side, contracts, ask,
            client_order_id=client_order_id,
        )
    except kalshi_gateway.KalshiError as exc:
        # The order was refused outright — nothing exists at the venue, so
        # the reserve goes straight back.
        logger.warning("kalshi accept: order refused on %s: %s", ticker, exc)
        pool.finish_kalshi_open(
            ref, filled_contracts=0, entry_cents=0, cost_usd=0, fee_usd=0,
            order_id=None,
        )
        return {**base, "ok": False, "reason": "order_refused"}

    order_id = str(order["order_id"])
    try:
        pool.attach_kalshi_order(ref, order_id)
    except Exception:
        logger.exception("kalshi accept: could not attach order_id %s", order_id)

    # Prefer booking from whatever the venue already told us. Create-body
    # fill fields are often empty on V2 even when status is executed — go
    # straight to /fills before any GET-by-id poll that can 404.
    summary = kalshi_gateway.summary_from_order(order, side)
    if summary is None or int(summary.get("contracts") or 0) <= 0:
        try:
            summary = kalshi_gateway.fill_summary(order_id, side)
        except kalshi_gateway.KalshiError:
            summary = None
        if summary is None or (
            int(summary.get("contracts") or 0) <= 0
            and not kalshi_gateway.order_is_terminal(order)
        ):
            try:
                for _ in range(_FILL_POLL_TRIES):
                    state = None
                    try:
                        state = kalshi_gateway.get_order_retry(
                            order_id, tries=2, sleep_sec=0.35,
                        )
                    except kalshi_gateway.KalshiError:
                        state = kalshi_gateway.find_order_by_client_id(
                            client_order_id
                        )
                    if state is not None and kalshi_gateway.order_is_terminal(state):
                        summary = (
                            kalshi_gateway.summary_from_order(state, side)
                            or kalshi_gateway.fill_summary(order_id, side)
                        )
                        break
                    time.sleep(_FILL_POLL_SLEEP_SEC)
                else:
                    try:
                        kalshi_gateway.cancel_order(order_id)
                    except kalshi_gateway.KalshiError:
                        pass
                    summary = kalshi_gateway.fill_summary(order_id, side)
            except kalshi_gateway.KalshiError as exc:
                logger.error(
                    "kalshi accept: fill state unknown for %s: %s", order_id, exc
                )
                # Keep placing + order_id for recover_placing_sweep.
                return {**base, "ok": False, "reason": "fill_unknown",
                        "order_id": order_id}

    if summary is None:
        logger.error("kalshi accept: no fill summary for %s", order_id)
        return {**base, "ok": False, "reason": "fill_unknown",
                "order_id": order_id}

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
        return {**base, "ok": False, "reason": "booking_failed",
                "order_id": order_id}

    if int(summary["contracts"]) <= 0:
        return {**base, "ok": False, "reason": "unfilled"}
    return {
        **base,
        "ok": True,
        "contracts": int(summary["contracts"]),
        "avg_cents": float(summary["avg_cents"]),
        "cost_usd": float(summary["cost_usd"]),
        "fee_usd": float(summary["fee_usd"]),
        "order_id": order_id,
    }


# ---------------------------------------------------------------------------
# Autopilot: a subscriber who flipped it on rides every trade the lane takes,
# without the tap. Each sweep entry is still that one user's own position —
# same resolve/re-quote/slip/reserve/place/book path as a manual Accept, same
# sizing from their own lane allocation. Nothing is pooled.
# ---------------------------------------------------------------------------

# (position_id, telegram_id) pairs already attempted this process lifetime —
# stops the sweep re-quoting the same refusal every minute. A restart retries,
# where the pool's unique intent_ref refuses any true duplicate outright.
_autopilot_seen: set[tuple[int, int]] = set()
_AUTOPILOT_SEEN_MAX = 4000

# Transient venue hiccups worth retrying on a later sweep while the window
# is still fresh; every other refusal is final for that (entry, user).
_AUTOPILOT_RETRY_REASONS = {"quote_failed", "no_quote"}

# The sweep now runs every few seconds (its own job, not the 60s watchdog
# scan), so a retryable refusal holds off this long before re-quoting —
# otherwise one no_quote market would be polled dozens of times a minute.
_AUTOPILOT_RETRY_DELAY_SEC = 30.0
_autopilot_retry_at: dict[tuple[int, int], float] = {}


def fresh_entries(strategy: str) -> list[int]:
    """Position ids of the lane bot's open entries still inside the accept
    freshness gate — the same recency rule a manual Accept is held to."""
    bot_id = STRATEGY_BOTS.get(strategy)
    db = kalshi_bridge.kalshi_db_path()
    if not bot_id or db is None or not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT id, opened_at FROM paper_positions WHERE bot_id = ? AND "
            "status = 'open' ORDER BY opened_at DESC LIMIT 8",
            (bot_id,),
        ).fetchall()
    except sqlite3.Error:
        logger.exception("kalshi autopilot: ledger query failed")
        return []
    finally:
        conn.close()
    max_age = float(bot_config.KALSHI_ACCEPT_MAX_AGE_MIN)
    out: list[int] = []
    for row in rows:
        age = _age_minutes(str(row["opened_at"]))
        if age is not None and age <= max_age:
            out.append(int(row["id"]))
    return out


def autopilot_sweep() -> list[dict[str, Any]]:
    """Enter every autopilot user into each fresh live entry; returns one
    result per attempt (the `accept` result plus who/what it was for)."""
    if not enabled():
        return []
    results: list[dict[str, Any]] = []
    for strategy in STRATEGY_BOTS:
        users = pool.autopilot_user_ids(strategy)
        if not users:
            continue
        for position_id in fresh_entries(strategy):
            for telegram_id in users:
                key = (position_id, telegram_id)
                if key in _autopilot_seen:
                    continue
                if time.monotonic() < _autopilot_retry_at.get(key, 0.0):
                    continue  # retryable refusal still cooling off
                try:
                    result = accept(telegram_id, strategy, str(position_id))
                except Exception:  # noqa: BLE001 — one user must not stall the rest
                    logger.exception(
                        "kalshi autopilot: accept crashed for %s on #%s",
                        telegram_id, position_id,
                    )
                    result = {"ok": False, "reason": "error"}
                if str(result.get("reason")) in _AUTOPILOT_RETRY_REASONS:
                    _autopilot_retry_at[key] = (
                        time.monotonic() + _AUTOPILOT_RETRY_DELAY_SEC
                    )
                else:
                    _autopilot_seen.add(key)
                    _autopilot_retry_at.pop(key, None)
                if result.get("reason") == "already_recorded":
                    # Manual Accept before the sweep got there — not news.
                    continue
                # Journal the attempt — fills and refusals alike — so the
                # slip gate can one day be set from a measured distribution.
                try:
                    pool.record_kalshi_attempt(
                        telegram_id, strategy, position_id,
                        outcome=(
                            "filled" if result.get("ok")
                            else str(result.get("reason") or "error")
                        ),
                        market_ticker=result.get("market_ticker"),
                        side=result.get("side"),
                        entry_cents=result.get("entry_cents"),
                        ask_cents=result.get("ask_cents"),
                        lag_sec=result.get("lag_sec"),
                    )
                except Exception:
                    logger.exception("kalshi autopilot: attempt journal failed")
                results.append({
                    "telegram_id": telegram_id,
                    "strategy": strategy,
                    "position_id": position_id,
                    **result,
                })
    if len(_autopilot_seen) > _AUTOPILOT_SEEN_MAX:
        _autopilot_seen.clear()
        _autopilot_retry_at.clear()
    return results


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


def recover_placing_sweep(*, min_age_sec: float = 20.0) -> list[dict[str, Any]]:
    """Finish 'placing' rows whose venue orders already filled but weren't booked.

    After a V2 create, ``GET /portfolio/orders/{id}`` can 404 for a few hundred
    ms even though the create body (and the eventual list endpoint) show the
    fill. Accept stamps ``order_id`` immediately and leaves the row placing on
    ``fill_unknown``; this sweep closes that gap without a human.

    Rows that somehow lost ``order_id`` are matched by ticker against recent
    ``eva-*`` client orders (hub Accepts always use that prefix).
    """
    if not kalshi_gateway.configured():
        return []
    recovered: list[dict[str, Any]] = []
    for row in pool.open_kalshi_rows("placing"):
        age_min = _age_minutes(str(row.get("created_at") or ""))
        if age_min is None or age_min * 60.0 < float(min_age_sec):
            continue
        intent_ref = str(row["intent_ref"])
        side = str(row["side"]).lower()
        order_id = row.get("order_id")
        order: dict[str, Any] | None = None
        if order_id:
            try:
                order = kalshi_gateway.get_order_retry(
                    str(order_id), tries=4, sleep_sec=0.4,
                )
            except kalshi_gateway.KalshiError:
                order = None
                try:
                    # List endpoint often sees the order before GET-by-id does.
                    payload = kalshi_gateway._request(
                        "GET", "/portfolio/orders",
                        params={"ticker": str(row["market_ticker"]), "limit": 20},
                    )
                    for cand in payload.get("orders") or []:
                        if str(cand.get("order_id")) == str(order_id):
                            order = cand
                            break
                except kalshi_gateway.KalshiError:
                    order = None
        if order is None:
            # Lost order_id (or never stamped): find the hub's eva-* order on
            # this ticker closest to the placing row's created_at.
            order = _find_hub_order_for_placing(row)
            if order is not None:
                order_id = str(order.get("order_id") or "") or order_id
                if order_id:
                    try:
                        pool.attach_kalshi_order(intent_ref, str(order_id))
                    except Exception:
                        logger.exception(
                            "kalshi recover: attach order_id %s failed", order_id
                        )
        if order is None and not order_id:
            continue
        summary = None
        if order is not None:
            summary = kalshi_gateway.summary_from_order(order, side)
        if (summary is None or int(summary.get("contracts") or 0) <= 0) and order_id:
            try:
                summary = kalshi_gateway.fill_summary(str(order_id), side)
            except kalshi_gateway.KalshiError:
                continue
        if summary is None:
            continue
        booked = pool.finish_kalshi_open(
            intent_ref,
            filled_contracts=int(summary["contracts"]),
            entry_cents=float(summary["avg_cents"]),
            cost_usd=float(summary["cost_usd"]),
            fee_usd=float(summary["fee_usd"]),
            order_id=str(order_id) if order_id else None,
        )
        if booked.get("ok"):
            recovered.append({
                "telegram_id": int(row["telegram_id"]),
                "strategy": str(row["strategy"]),
                "market_ticker": str(row["market_ticker"]),
                "side": side,
                "intent_ref": intent_ref,
                "order_id": str(order_id or ""),
                **summary,
                "ok": True,
            })
            logger.info(
                "kalshi recover: booked placing #%s → %s x%s @ %.0f¢",
                row["id"], side, summary["contracts"], summary["avg_cents"],
            )
    return recovered


def _find_hub_order_for_placing(row: dict[str, Any]) -> dict[str, Any] | None:
    """Match a placing row to a recent hub ``eva-*`` order on the same ticker.

    Used when ``order_id`` was never stamped. Picks the closest-in-time
    terminal order whose client_order_id starts with ``eva-``.
    """
    ticker = str(row.get("market_ticker") or "")
    if not ticker:
        return None
    created = str(row.get("created_at") or "")
    try:
        payload = kalshi_gateway._request(
            "GET", "/portfolio/orders",
            params={"ticker": ticker, "limit": 50},
        )
    except kalshi_gateway.KalshiError:
        return None
    best: dict[str, Any] | None = None
    best_dt = None
    target_dt = None
    if created:
        try:
            target_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            target_dt = None
    for cand in payload.get("orders") or []:
        cid = str(cand.get("client_order_id") or "")
        if not cid.startswith("eva-"):
            continue
        if not kalshi_gateway.order_is_terminal(cand):
            continue
        cts = str(cand.get("created_time") or cand.get("created_at") or "")
        cand_dt = None
        if cts:
            try:
                cand_dt = datetime.fromisoformat(cts.replace("Z", "+00:00"))
            except ValueError:
                cand_dt = None
        if best is None:
            best, best_dt = cand, cand_dt
            continue
        if target_dt is not None and cand_dt is not None:
            if best_dt is None or abs((cand_dt - target_dt).total_seconds()) < abs(
                (best_dt - target_dt).total_seconds()
            ):
                best, best_dt = cand, cand_dt
    return best
