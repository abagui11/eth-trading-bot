"""Treasury-lite: where client capital physically sits, and every move of it.

Phase 1 of the pooled-capital plan. Three locations — the shared test wallet
(on-chain USDC), the Coinbase account, and the Kalshi account — plus transfers
in flight between them. Two jobs:

1. **A journal of transfers.** Every movement is a row that was intended,
   sent, and seen to arrive. A transfer that is 'sent' but never confirms is
   the alarm condition. Who sends: an operator by hand (`/transfer_sent`),
   or — only for `test_wallet → venue` legs, only when `signer.py` is
   configured, only on an admin tap — the bot itself via `execute_transfer`.
2. **The reconcile total.** The fiduciary invariant becomes: user claims ≤
   test wallet + Coinbase + Kalshi + in-flight. `reconcile_total` produces
   that sum, and refuses to produce it at all when any configured leg cannot
   be read — "we could not check" must never read as "the money is gone",
   and equally never as "everything is fine".

`execute_transfer` is the single path in this module that moves money. It
claims the journal row atomically before broadcasting so a leg can never go
out twice, and everything else here observes, journals, and refuses.
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


# ---------------------------------------------------------------------------
# Signer execution — the one path that moves money, and its guardrails
# ---------------------------------------------------------------------------

_SIGNER_NOTE_TAG = "[signer]"


def signer_sent_usd_last_24h() -> float:
    """Dollars the signer has broadcast in the trailing 24h (sent or confirmed)."""
    from datetime import timedelta

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with _connect() as conn:
        row = conn.execute(
            "SELECT SUM(amount_usd) AS s FROM treasury_transfers "
            "WHERE status IN ('sent', 'confirmed') AND sent_at >= ? "
            "AND note LIKE ?",
            (since, f"%{_SIGNER_NOTE_TAG}%"),
        ).fetchone()
    return float(row["s"] or 0.0)


def signer_check(transfer_id: int) -> dict[str, Any]:
    """Would the signer accept this leg? Pure read — nothing changes.

    The Send button is only drawn when this says ok, and `execute_transfer`
    re-runs the same checks under the claim, so a stale card cannot send
    something the policy no longer allows.
    """
    import signer

    row = get_transfer(transfer_id)
    if row is None:
        return {"ok": False, "reason": "not_found"}
    if str(row["status"]) != "pending_send":
        return {"ok": False, "reason": "not_pending", "status": row["status"]}
    if str(row["from_loc"]) != "test_wallet":
        return {"ok": False, "reason": "not_from_test_wallet"}
    st = signer.status()
    if not st.get("enabled"):
        return {"ok": False, "reason": f"signer_{st.get('reason')}"}
    dest = signer.destination(str(row["to_loc"]))
    if dest is None:
        return {"ok": False, "reason": "no_allowlisted_destination",
                "to_loc": row["to_loc"]}
    amount = float(row["amount_usd"])
    per_leg = float(getattr(config, "TREASURY_SEND_MAX_USD", 0) or 0)
    if per_leg > 0 and amount > per_leg + 1e-9:
        return {"ok": False, "reason": "over_leg_cap", "cap_usd": per_leg,
                "amount_usd": amount}
    daily = float(getattr(config, "TREASURY_SEND_DAILY_MAX_USD", 0) or 0)
    used = signer_sent_usd_last_24h()
    if daily > 0 and used + amount > daily + 1e-9:
        return {"ok": False, "reason": "over_daily_cap", "cap_usd": daily,
                "used_usd": round(used, 2), "amount_usd": amount}
    return {"ok": True, "amount_usd": amount, "to_loc": str(row["to_loc"]),
            "destination": dest}


def _claim_for_send(transfer_id: int, *, admin_id: int) -> bool:
    """Atomically flip pending_send → sent (no txid yet) so no second tap,
    thread, or process can broadcast the same leg. Returns False if someone
    else already claimed it."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE treasury_transfers SET status = 'sent', sent_at = ?, "
            "note = COALESCE(note, '') || ? "
            "WHERE id = ? AND status = 'pending_send'",
            (_now(), f" {_SIGNER_NOTE_TAG} broadcasting by {int(admin_id)}",
             int(transfer_id)),
        )
        return cur.rowcount == 1


def _release_claim(transfer_id: int, *, error: str) -> None:
    """Broadcast failed before any tx hash existed: hand the leg back."""
    with _connect() as conn:
        conn.execute(
            "UPDATE treasury_transfers SET status = 'pending_send', sent_at = NULL, "
            "note = REPLACE(COALESCE(note, ''), ?, '') || ? "
            "WHERE id = ? AND status = 'sent' AND txid IS NULL",
            (f" {_SIGNER_NOTE_TAG} broadcasting", f" [signer failed: {error[:160]}]",
             int(transfer_id)),
        )


def execute_transfer(transfer_id: int, *, admin_id: int) -> dict[str, Any]:
    """Sign and broadcast one journaled test_wallet → venue leg.

    Order matters: policy check → gas top-up if needed → atomic claim →
    broadcast → record txid. A failure anywhere before the broadcast
    releases the claim; a failure *after* the node accepted the tx keeps
    the row 'sent' (money moved) and records the hash.
    """
    import signer

    check = signer_check(transfer_id)
    if not check.get("ok"):
        return check
    dest = check["destination"]
    amount = float(check["amount_usd"])
    # Best-effort: buy ETH from free USDC when the buffer is low. Never spend
    # into the leg itself or into other users' undeployed claims. A failed
    # top-up must NOT block the venue send — send_usdc still refuses if
    # there truly is not enough gas for the transfer itself.
    gas = {"ok": True, "skipped": "not_attempted"}
    try:
        import pool as _pool

        undeployed = float(_pool.undeployed_claims_usd())
    except Exception:
        undeployed = 0.0
    try:
        gas = signer.ensure_gas(
            int(dest["chain_id"]),
            reserve_usdc=amount + undeployed,
        )
        if not gas.get("ok"):
            logger.warning(
                "treasury: gas top-up for #%d did not run (%s) — continuing to send",
                transfer_id, gas.get("detail") or gas.get("reason"),
            )
    except Exception:
        logger.exception(
            "treasury: gas top-up for #%d crashed — continuing to send", transfer_id
        )
        gas = {"ok": False, "reason": "gas_topup_crashed"}
    if not _claim_for_send(transfer_id, admin_id=admin_id):
        row = get_transfer(transfer_id) or {}
        return {"ok": False, "reason": "already_claimed",
                "status": row.get("status"), "txid": row.get("txid")}
    try:
        sent = signer.send_usdc(str(check["to_loc"]), amount)
    except signer.SignerError as exc:
        detail = str(exc)
        if "not enough gas" in detail.lower():
            # Last chance: try a top-up again, then one more send.
            retry_gas = signer.ensure_gas(int(dest["chain_id"]), reserve_usdc=amount)
            if retry_gas.get("ok") and retry_gas.get("topped_up"):
                try:
                    sent = signer.send_usdc(str(check["to_loc"]), amount)
                    gas = retry_gas
                except signer.SignerError as exc2:
                    _release_claim(transfer_id, error=str(exc2))
                    logger.warning(
                        "treasury: transfer #%d signer refused after gas retry: %s",
                        transfer_id, exc2,
                    )
                    return {"ok": False, "reason": "signer_refused",
                            "detail": str(exc2), "gas_topup": retry_gas}
            else:
                _release_claim(transfer_id, error=detail)
                logger.warning(
                    "treasury: transfer #%d signer refused: %s", transfer_id, exc
                )
                return {"ok": False, "reason": "signer_refused", "detail": detail,
                        "gas_topup": retry_gas}
        else:
            _release_claim(transfer_id, error=detail)
            logger.warning("treasury: transfer #%d signer refused: %s", transfer_id, exc)
            return {"ok": False, "reason": "signer_refused", "detail": detail}
    except Exception as exc:  # unexpected: still release, never leave it stuck
        _release_claim(transfer_id, error=f"{type(exc).__name__}: {exc}")
        logger.exception("treasury: transfer #%d signer crashed", transfer_id)
        return {"ok": False, "reason": "signer_error", "detail": str(exc)}
    with _connect() as conn:
        conn.execute(
            "UPDATE treasury_transfers SET txid = ?, "
            "note = REPLACE(COALESCE(note, ''), ?, ?) WHERE id = ?",
            (sent["txid"], f"{_SIGNER_NOTE_TAG} broadcasting",
             f"{_SIGNER_NOTE_TAG} sent", int(transfer_id)),
        )
    logger.info(
        "treasury: transfer #%d sent by signer (tx %s) on behalf of %s",
        transfer_id, sent["txid"], admin_id,
    )
    out = {"ok": True, "transfer_id": int(transfer_id), **sent}
    if gas.get("topped_up"):
        out["gas_topup"] = gas
    return out


def retry_pending_deploy_sends(*, admin_id: int = 0) -> list[dict[str, Any]]:
    """Re-attempt journaled deploy legs still sitting in pending_send.

    Skips legs the intake wallet still cannot cover (no point spamming the
    signer). Gas-related failures retry once ETH is topped up.
    """
    results: list[dict[str, Any]] = []
    for row in open_transfers():
        if str(row.get("status")) != "pending_send":
            continue
        if str(row.get("from_loc")) != "test_wallet":
            continue
        note = str(row.get("note") or "")
        if not note.startswith("deploy "):
            continue
        to_loc = str(row["to_loc"])
        amount = float(row["amount_usd"])
        cov = deployable_usd(to_loc)
        # This pending row is already in pending_out. Rebuild room from the
        # components — deployable_usd is floored at 0, so adding `amount`
        # back onto that floor would falsely claim coverage when on-chain
        # is short by more than the floor clipped away.
        if cov.get("ok"):
            room = (
                float(cov.get("on_chain_usd") or 0)
                - float(cov.get("pending_out_usd") or 0)
                + amount
                - float(cov.get("undeployed_claims_usd") or 0)
                - float(cov.get("gas_reserve_usd") or 0)
            )
        else:
            room = -1.0
        if cov.get("ok") and room + 1e-9 < amount:
            results.append({
                "transfer_id": int(row["id"]),
                "ok": False,
                "reason": "intake_short",
                "skipped": True,
                "amount_usd": amount,
                "to_loc": to_loc,
                "deployable_usd": float(cov.get("deployable_usd") or 0),
                "note": note,
            })
            continue
        result = execute_transfer(int(row["id"]), admin_id=int(admin_id))
        results.append({"transfer_id": int(row["id"]), **result,
                        "amount_usd": amount,
                        "to_loc": to_loc,
                        "note": note})
    return results


def confirm_sweep() -> list[dict[str, Any]]:
    """Auto-confirm 'sent' transfers whose arrival the chain can prove.

    Only legs with a chain-visible destination and a recorded txid: the
    transfer's own hash found at the destination is the same standard of
    evidence deposits are held to. Kalshi legs stay 'sent' until an admin
    confirms — a silent auto-confirm there would be a guess.
    """
    import chain

    chain_ids = tuple(dict.fromkeys(
        [*map(int, config.TEST_WALLET_CHAIN_IDS), chain.CHAIN_ID]
    ))
    if not any(chain.readable(c) for c in chain_ids):
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
        elif dest == "kalshi":
            # Chain-visible only once the Kalshi USDC deposit address is
            # configured; without it the leg still needs /transfer_confirm.
            to_address = getattr(config, "KALSHI_DEPOSIT_ADDRESS", None)
        else:
            continue
        if not to_address:
            continue
        found = None
        for chain_id in chain_ids:
            if not chain.readable(chain_id):
                continue
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

        # One EOA across every watched chain; the leg is the sum. Any chain
        # unreadable makes the whole leg unknown — a partial sum would read
        # as money missing to the reconciler.
        total = 0.0
        per_chain: dict[str, float] = {}
        for chain_id in config.TEST_WALLET_CHAIN_IDS:
            try:
                usd = chain.usdc_balance(
                    str(config.TEST_WALLET_ADDRESS), chain_id=int(chain_id),
                )
            except Exception as exc:  # noqa: BLE001 — any failed read is "unknown"
                leg["error"] = f"{chain.chain_name(int(chain_id))}: {str(exc)[:160]}"
                break
            per_chain[chain.chain_name(int(chain_id))] = round(usd, 2)
            total += usd
        else:
            leg["usd"] = round(total, 2)
            leg["per_chain"] = per_chain
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


def kalshi_coverage() -> dict[str, Any]:
    """House Kalshi balance vs total user Kalshi lane demand.

    Demand is an upper bound (allocations, not spent cash). A positive
    shortfall means Accepts can still size against the user's journal but
    the venue may refuse when the house account is empty — the operator
    must top up.
    """
    demand = float(venue_demand().get("kalshi") or 0.0)
    leg = balances().get("kalshi") or {}
    if not leg.get("configured"):
        return {
            "ok": False,
            "reason": "kalshi_unconfigured",
            "demand_usd": demand,
            "balance_usd": None,
            "in_flight_usd": 0.0,
            "shortfall_usd": demand,
        }
    if leg.get("usd") is None:
        return {
            "ok": False,
            "reason": "kalshi_unreadable",
            "demand_usd": demand,
            "balance_usd": None,
            "in_flight_usd": 0.0,
            "shortfall_usd": None,
            "detail": leg.get("error"),
        }
    inbound = 0.0
    for row in open_transfers():
        if str(row.get("to_loc")) != "kalshi":
            continue
        if str(row.get("status")) not in ("pending_send", "sent"):
            continue
        inbound += float(row.get("amount_usd") or 0.0)
    covered = float(leg["usd"]) + inbound
    shortfall = max(0.0, round(demand - covered, 2))
    return {
        "ok": True,
        "demand_usd": round(demand, 2),
        "balance_usd": round(float(leg["usd"]), 2),
        "in_flight_usd": round(inbound, 2),
        "shortfall_usd": shortfall,
    }


def ensure_kalshi_coverage(
    *, admin_id: int, note: str | None = None
) -> dict[str, Any]:
    """If Kalshi demand exceeds balance + in-flight, journal a top-up.

    Idempotent against an already-open pending_send/sent transfer to kalshi:
    we do not stack another request while one is in flight. Returns the
    coverage snapshot plus `transfer_id` when a new row was created (or the
    existing open one when in-flight already covers the gap).
    """
    gap = kalshi_coverage()
    if not gap.get("ok"):
        return gap

    open_to_kalshi = [
        row for row in open_transfers()
        if (
            str(row.get("to_loc")) == "kalshi"
            and str(row.get("status")) in ("pending_send", "sent")
        )
    ]
    shortfall = float(gap.get("shortfall_usd") or 0.0)
    if open_to_kalshi:
        row = open_to_kalshi[0]
        return {
            **gap,
            "transfer_id": int(row["id"]),
            "created": False,
            "from_loc": str(row.get("from_loc")),
        }
    if shortfall <= 0:
        return {**gap, "transfer_id": None, "created": False}

    # Prefer topping up from the test wallet when it is the intake address;
    # otherwise from coinbase — both are journal-only intentions.
    from_loc = "test_wallet" if config.TEST_WALLET_ADDRESS else "coinbase"
    result = request_transfer(
        from_loc, "kalshi", shortfall, admin_id=admin_id, note=note
    )
    if not result.get("ok"):
        return {**gap, "transfer_id": None, "created": False,
                "transfer_error": result.get("reason")}
    return {
        **gap,
        "transfer_id": int(result["transfer_id"]),
        "created": True,
        "from_loc": from_loc,
    }


def journal_deploy_move(
    *, telegram_id: int, strategy: str, delta_usd: float, admin_id: int,
) -> dict[str, Any]:
    """Journal the venue move implied by one allocation change.

    Rule: deployed money lives at the strategy's venue; undeployed money
    stays in the intake (test) wallet. So a deploy of +$X journals
    test_wallet -> venue for $X, and an undeploy of -$X journals the
    reverse. Journal-only: the operator (or a signer, if one is wired)
    still has to move the funds and `/transfer_sent` it.

    A deploy is refused with ``intake_short`` when the test wallet cannot
    physically cover it (on-chain USDC minus other pending outs minus
    undeployed tester claims). Callers must roll the soft-lock back.
    """
    delta = round(float(delta_usd or 0.0), 2)
    if abs(delta) < 0.01:
        return {"ok": False, "reason": "no_change"}
    venue = STRATEGY_VENUES.get(str(strategy))
    if not venue:
        return {"ok": False, "reason": "no_venue"}
    if not config.TEST_WALLET_ADDRESS:
        return {"ok": False, "reason": "no_intake_wallet"}
    if delta > 0:
        from_loc, to_loc, amount = "test_wallet", venue, delta
        verb = "deploy"
        cov = deployable_usd(venue)
        if not cov.get("ok"):
            return {
                "ok": False,
                "reason": "intake_unreadable",
                "venue": venue,
                **{k: cov.get(k) for k in (
                    "detail", "on_chain_usd", "pending_out_usd",
                    "undeployed_claims_usd", "deployable_usd",
                )},
                "need_usd": amount,
            }
        if float(cov.get("deployable_usd") or 0) + 1e-9 < amount:
            return {
                "ok": False,
                "reason": "intake_short",
                "venue": venue,
                "need_usd": amount,
                "deployable_usd": float(cov["deployable_usd"]),
                "on_chain_usd": float(cov.get("on_chain_usd") or 0),
                "pending_out_usd": float(cov.get("pending_out_usd") or 0),
                "undeployed_claims_usd": float(
                    cov.get("undeployed_claims_usd") or 0
                ),
            }
    else:
        from_loc, to_loc, amount = venue, "test_wallet", -delta
        verb = "undeploy"
    note = f"{verb} {strategy} user {int(telegram_id)}"
    result = request_transfer(
        from_loc, to_loc, amount, admin_id=admin_id, note=note
    )
    if not result.get("ok"):
        return {**result, "venue": venue}
    return {
        "ok": True,
        "transfer_id": int(result["transfer_id"]),
        "from_loc": from_loc,
        "to_loc": to_loc,
        "amount_usd": round(amount, 2),
        "venue": venue,
        "verb": verb,
    }


def venue_deposit_chain_id(venue: str) -> int | None:
    if venue == "kalshi":
        cid = getattr(config, "KALSHI_DEPOSIT_CHAIN_ID", None)
    elif venue == "coinbase":
        cid = getattr(config, "POOL_DEPOSIT_CHAIN_ID", None)
    else:
        return None
    return int(cid) if cid is not None else None


def deployable_usd(venue: str) -> dict[str, Any]:
    """USDC the test wallet can still send to ``venue`` without stranding anyone.

    ``on_chain − pending_outbound − undeployed_claims``. Call *after* the
    soft-lock allocation is written so this deploy's dollars are no longer
    counted as undeployed.
    """
    import chain
    import pool

    cid = venue_deposit_chain_id(venue)
    if cid is None or not config.TEST_WALLET_ADDRESS:
        return {"ok": False, "reason": "no_venue_chain", "deployable_usd": 0.0}
    try:
        on_chain = float(
            chain._usdc_balance_rpc(config.TEST_WALLET_ADDRESS, chain_id=int(cid))
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": "unreadable",
            "detail": str(exc),
            "deployable_usd": 0.0,
        }
    pending_out = 0.0
    for row in open_transfers():
        if str(row.get("from_loc")) != "test_wallet":
            continue
        if str(row.get("status")) != "pending_send":
            continue
        pending_out += float(row.get("amount_usd") or 0.0)
    undeployed = float(pool.undeployed_claims_usd())
    gas_reserve = float(getattr(config, "TREASURY_GAS_RESERVE_USD", 0) or 0)
    deployable = round(on_chain - pending_out - undeployed - gas_reserve, 2)
    return {
        "ok": True,
        "deployable_usd": max(0.0, deployable),
        "on_chain_usd": round(on_chain, 2),
        "pending_out_usd": round(pending_out, 2),
        "undeployed_claims_usd": round(undeployed, 2),
        "gas_reserve_usd": round(gas_reserve, 2),
        "chain_id": int(cid),
    }


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
            split = leg.get("per_chain") or {}
            detail = (
                " (" + ", ".join(f"{k} ${v:,.2f}" for k, v in split.items()) + ")"
                if len(split) > 1 else ""
            )
            lines.append(f"• {name}: ${leg['usd']:,.2f}{detail}")
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
    lines += ["", _signer_line()]
    lines += [
        "",
        "Commands: /transfer <from> <to> <amount> · /transfer_send <id> · "
        "/transfer_sent <id> <txid> · /transfer_confirm <id> · /transfer_cancel <id>",
    ]
    return "\n".join(lines)


def _signer_line() -> str:
    try:
        import chain
        import signer

        st = signer.status()
        if not st.get("enabled"):
            return f"Signer: off ({st.get('reason')}) — legs are sent by hand."
        targets = []
        for venue in ("coinbase", "kalshi"):
            dest = signer.destination(venue)
            targets.append(
                f"{venue} on {chain.chain_name(dest['chain_id'])}" if dest
                else f"{venue} (no deposit address)"
            )
        used = signer_sent_usd_last_24h()
        return (
            f"Signer: on · {' · '.join(targets)} · caps "
            f"${float(config.TREASURY_SEND_MAX_USD):,.0f}/leg, "
            f"${float(config.TREASURY_SEND_DAILY_MAX_USD):,.0f}/24h "
            f"(${used:,.2f} used)"
        )
    except Exception as exc:  # the report must never fail on this line
        return f"Signer: status unavailable ({exc})"
