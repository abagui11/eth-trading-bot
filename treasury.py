"""Treasury-lite: where client capital physically sits, and every move of it.

Phase 1 of the pooled-capital plan. Three locations — the shared test wallet
(on-chain USDC), the Coinbase account, and the Kalshi account — plus transfers
in flight between them. Two jobs:

1. **A journal of transfers.** The test wallet's key never touches this box,
   so the bot cannot move funds; an operator does, and this table is the
   record that a movement was intended, sent, and seen to arrive. A transfer
   that is 'sent' but never confirms is the alarm condition.
2. **The reconcile total.** The fiduciary invariant becomes: user claims ≤
   test wallet + Coinbase + Kalshi + in-flight. `reconcile_total` produces
   that sum, and refuses to produce it at all when any configured leg cannot
   be read — "we could not check" must never read as "the money is gone",
   and equally never as "everything is fine".

Nothing in this module moves money. It observes, journals, and refuses.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config

logger = logging.getLogger(__name__)

LOCATIONS = ("test_wallet", "coinbase", "kalshi")

# Which venue each strategy's allocations draw on, for the demand view.
STRATEGY_VENUES = {
    "ict": "coinbase",
    "mill": "coinbase",
    "kalshi_reversal": "kalshi",
    "kalshi_wick": "kalshi",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS treasury_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_loc TEXT NOT NULL,
    to_loc TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_send',
        -- pending_send | sent | confirmed | cancelled
    txid TEXT,
    note TEXT,
    requested_by INTEGER,
    requested_at TEXT NOT NULL,
    sent_at TEXT,
    confirmed_at TEXT,
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS treasury_transfers_status
    ON treasury_transfers (status);
"""

_LOCK_TIMEOUT_SEC = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB, timeout=_LOCK_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Transfer journal
# ---------------------------------------------------------------------------

def request_transfer(
    from_loc: str, to_loc: str, amount_usd: float,
    *, admin_id: int, note: str | None = None,
) -> dict[str, Any]:
    if from_loc not in LOCATIONS or to_loc not in LOCATIONS:
        return {"ok": False, "reason": "bad_location", "locations": LOCATIONS}
    if from_loc == to_loc:
        return {"ok": False, "reason": "same_location"}
    if not amount_usd or amount_usd <= 0:
        return {"ok": False, "reason": "bad_amount"}
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO treasury_transfers (from_loc, to_loc, amount_usd, "
            "note, requested_by, requested_at) VALUES (?, ?, ?, ?, ?, ?)",
            (from_loc, to_loc, round(float(amount_usd), 2), note,
             int(admin_id), _now()),
        )
        transfer_id = int(cur.lastrowid)
    logger.info(
        "treasury: transfer #%d requested %s -> %s $%.2f by %s",
        transfer_id, from_loc, to_loc, amount_usd, admin_id,
    )
    return {"ok": True, "transfer_id": transfer_id}


def get_transfer(transfer_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM treasury_transfers WHERE id = ?", (int(transfer_id),)
        ).fetchone()
    return dict(row) if row else None


def mark_sent(transfer_id: int, *, txid: str | None, admin_id: int) -> dict[str, Any]:
    """The operator has actually moved the funds; record the evidence."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM treasury_transfers WHERE id = ?",
            (int(transfer_id),),
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if str(row["status"]) != "pending_send":
            return {"ok": False, "reason": "not_pending", "status": row["status"]}
        conn.execute(
            "UPDATE treasury_transfers SET status = 'sent', txid = ?, "
            "sent_at = ? WHERE id = ?",
            (txid, _now(), int(transfer_id)),
        )
    logger.info("treasury: transfer #%d sent (tx %s) by %s", transfer_id, txid, admin_id)
    return {"ok": True}


def confirm_transfer(transfer_id: int, *, admin_id: int | None = None,
                     note: str | None = None) -> dict[str, Any]:
    """Arrival seen — by the chain sweep, or by an admin for legs the chain
    cannot see (Kalshi's funding rail is not chain-visible from here)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM treasury_transfers WHERE id = ?",
            (int(transfer_id),),
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if str(row["status"]) == "confirmed":
            return {"ok": True, "already": True}
        if str(row["status"]) != "sent":
            return {"ok": False, "reason": "not_sent", "status": row["status"]}
        conn.execute(
            "UPDATE treasury_transfers SET status = 'confirmed', "
            "confirmed_at = ?, note = COALESCE(?, note) WHERE id = ?",
            (_now(), note, int(transfer_id)),
        )
    logger.info(
        "treasury: transfer #%d confirmed%s",
        transfer_id, f" by {admin_id}" if admin_id else " on-chain",
    )
    return {"ok": True}


def cancel_transfer(transfer_id: int, *, admin_id: int) -> dict[str, Any]:
    """Withdraw an intention that was never acted on. 'sent' cannot be
    cancelled — money that moved has to be confirmed or investigated."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM treasury_transfers WHERE id = ?",
            (int(transfer_id),),
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if str(row["status"]) != "pending_send":
            return {"ok": False, "reason": "not_pending", "status": row["status"]}
        conn.execute(
            "UPDATE treasury_transfers SET status = 'cancelled', "
            "cancelled_at = ? WHERE id = ?",
            (_now(), int(transfer_id)),
        )
    logger.info("treasury: transfer #%d cancelled by %s", transfer_id, admin_id)
    return {"ok": True}


def open_transfers() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM treasury_transfers WHERE status IN "
            "('pending_send', 'sent') ORDER BY requested_at"
        ).fetchall()
    return [dict(r) for r in rows]


def in_flight_usd() -> float:
    """Money that left one location and has not been seen at the other."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT SUM(amount_usd) AS s FROM treasury_transfers "
            "WHERE status = 'sent'"
        ).fetchone()
    return float(row["s"] or 0.0)


def confirm_sweep() -> list[dict[str, Any]]:
    """Auto-confirm 'sent' transfers whose arrival the chain can prove.

    Only legs with a chain-visible destination and a recorded txid: the
    transfer's own hash found at the destination is the same standard of
    evidence deposits are held to. Kalshi legs stay 'sent' until an admin
    confirms — a silent auto-confirm there would be a guess.
    """
    import chain

    if not chain.configured():
        return []
    confirmed: list[dict[str, Any]] = []
    for transfer in open_transfers():
        if transfer["status"] != "sent" or not transfer.get("txid"):
            continue
        dest = str(transfer["to_loc"])
        if dest == "coinbase":
            to_address = config.POOL_DEPOSIT_ADDRESS
        elif dest == "test_wallet":
            to_address = config.TEST_WALLET_ADDRESS
        else:
            continue  # kalshi: not chain-visible, needs /transfer_confirm
        if not to_address:
            continue
        found = None
        for chain_id in {int(config.TEST_WALLET_CHAIN_ID), chain.CHAIN_ID}:
            try:
                found = chain.find_transfer(
                    str(transfer["txid"]), to_address=to_address,
                    chain_id=chain_id,
                )
            except chain.ChainError:
                continue
            if found:
                break
        if found and found["confirmations"] >= chain.MIN_CONFIRMATIONS:
            result = confirm_transfer(int(transfer["id"]))
            if result.get("ok"):
                confirmed.append({**transfer, "status": "confirmed"})
    return confirmed


# ---------------------------------------------------------------------------
# Balances and the reconcile total
# ---------------------------------------------------------------------------

def balances() -> dict[str, dict[str, Any]]:
    """Per-location USD, read fresh. Shape per leg:
    {configured, usd (None when unreadable), error}."""
    out: dict[str, dict[str, Any]] = {}

    leg: dict[str, Any] = {"configured": bool(config.TEST_WALLET_ADDRESS),
                           "usd": None, "error": None}
    if leg["configured"]:
        import chain

        try:
            leg["usd"] = round(chain.usdc_balance(
                str(config.TEST_WALLET_ADDRESS),
                chain_id=int(config.TEST_WALLET_CHAIN_ID),
            ), 2)
        except Exception as exc:  # noqa: BLE001 — any failed read is "unknown"
            leg["error"] = str(exc)[:200]
    out["test_wallet"] = leg

    leg = {"configured": config.EXECUTION_MODE == "live", "usd": None, "error": None}
    if leg["configured"]:
        try:
            from coinbase_deriv import get_gateway

            assets = get_gateway().get_cash_assets()
            total = float(assets.get("total_usd") or 0.0)
            if total > 0 and not assets.get("truncated"):
                leg["usd"] = round(total, 2)
            else:
                leg["error"] = "implausible read (zero or truncated)"
        except Exception as exc:  # noqa: BLE001
            leg["error"] = str(exc)[:200]
    out["coinbase"] = leg

    import kalshi_gateway

    leg = {"configured": kalshi_gateway.configured(), "usd": None, "error": None}
    if leg["configured"]:
        try:
            leg["usd"] = round(kalshi_gateway.get_balance_usd(), 2)
        except Exception as exc:  # noqa: BLE001
            leg["error"] = str(exc)[:200]
    out["kalshi"] = leg
    return out


def reconcile_total() -> dict[str, Any]:
    """The venue-assets figure for `pool.reconcile`, or a refusal.

    Every *configured* leg must read cleanly; one unreadable leg poisons the
    total, because a sum with a hole in it would either freeze the pool over
    an API hiccup or paper over real missing money depending on which way the
    hole points.
    """
    legs = balances()
    total = 0.0
    breakdown: dict[str, Any] = {}
    for name, leg in legs.items():
        if not leg["configured"]:
            continue
        if leg["usd"] is None:
            return {"ok": False, "reason": f"{name}_unreadable",
                    "detail": leg.get("error")}
        total += float(leg["usd"])
        breakdown[f"{name}_usd"] = leg["usd"]
    inflight = in_flight_usd()
    total += inflight
    breakdown["in_flight_usd"] = round(inflight, 2)
    return {"ok": True, "total_usd": round(total, 2), "breakdown": breakdown}


def venue_demand() -> dict[str, float]:
    """What each venue would need to cover every user allocation in full.

    An upper bound, deliberately: allocations are sizing bases, not spent
    cash, so real usage is far below this. It answers "which venue is the
    next transfer for", not "how much must move".
    """
    demand = {"coinbase": 0.0, "kalshi": 0.0}
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT strategy, SUM(amount_usd) AS s FROM pool_strategy_allocs "
                "GROUP BY strategy"
            ).fetchall()
    except sqlite3.Error:  # pool schema not initialized on this ledger yet
        return demand
    for row in rows:
        venue = STRATEGY_VENUES.get(str(row["strategy"]))
        if venue:
            demand[venue] += float(row["s"] or 0.0)
    return {k: round(v, 2) for k, v in demand.items()}


def report() -> str:
    """The /treasury text: balances, demand, open transfers, the invariant."""
    import pool

    legs = balances()
    lines = ["Treasury\n"]
    for name in LOCATIONS:
        leg = legs[name]
        if not leg["configured"]:
            lines.append(f"• {name}: not configured")
        elif leg["usd"] is None:
            lines.append(f"• {name}: UNREADABLE — {leg.get('error')}")
        else:
            lines.append(f"• {name}: ${leg['usd']:,.2f}")
    inflight = in_flight_usd()
    if inflight:
        lines.append(f"• in flight: ${inflight:,.2f}")

    demand = venue_demand()
    lines += [
        "",
        "Allocation demand (upper bound):",
        f"• coinbase: ${demand['coinbase']:,.2f}",
        f"• kalshi: ${demand['kalshi']:,.2f}",
    ]

    claims = pool.total_tester_cash()
    total = reconcile_total()
    lines.append("")
    if total.get("ok"):
        headroom = float(total["total_usd"]) - claims
        verdict = "OK" if headroom >= 0 else "SHORTFALL"
        lines.append(
            f"User claims ${claims:,.2f} vs assets ${total['total_usd']:,.2f} "
            f"→ {verdict} (${headroom:,.2f} headroom)"
        )
    else:
        lines.append(
            f"User claims ${claims:,.2f} vs assets: UNKNOWN "
            f"({total.get('reason')}) — no leg may be guessed."
        )

    open_rows = open_transfers()
    if open_rows:
        lines += ["", "Open transfers:"]
        for t in open_rows:
            tx = f" tx {str(t['txid'])[:10]}…" if t.get("txid") else ""
            lines.append(
                f"• #{t['id']} {t['from_loc']} → {t['to_loc']} "
                f"${float(t['amount_usd']):,.2f} [{t['status']}]{tx}"
            )
    lines += [
        "",
        "Commands: /transfer <from> <to> <amount> · /transfer_sent <id> <txid> · "
        "/transfer_confirm <id> · /transfer_cancel <id>",
    ]
    return "\n".join(lines)
