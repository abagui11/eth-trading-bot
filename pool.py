"""Tester pool — real deposits sharing the house Coinbase account.

Ten to twenty approved testers fund one venue account alongside the house.
Their Accepts join the house order as *pool intents*; when the order fires,
one aggregate market order fills and each participant owns a virtual pro-rata
share of it (``pool_stakes``). Exits ride the house ladder unchanged and each
booked leg credits every stake by its share. Nobody's slice ever needs to
afford a whole CDE nano contract by itself — pooling with the house budget is
what makes the contract exist.

Fiduciary rules (non-negotiable):
  - Cash only ever moves by appending a ``pool_events`` row. Every event
    carries the balance after it, so the journal alone reconstructs any
    account at any point in time.
  - Exit booking is idempotent by (telegram_id, kind, ref) — the same venue
    order id can be swept twice without double-crediting anyone.
  - Deposits are credited by an admin only after the funds are actually on
    the venue. The Coinbase balance is the source of truth for money in;
    this ledger attributes it.
  - ``reconcile`` checks the venue can cover every tester's realized claim.
    A shortfall freezes NEW intents and alerts ops; it never silently
    adjusts a balance.

Sizing: a tester's Accept contributes ``POOL_RISK_PCT × available cash`` of
risk budget. At fill time the extra contracts those budgets afford are added
to the house order and the actual fill is split pro-rata to budgets (house
budget included). If tester budgets cannot afford even one extra contract,
they still take their pro-rata slice of the house-sized clip — they dilute
the house rather than force a larger order, so an Accept honestly means
"you are in when it fills".
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import bot_config
import config

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approved_users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER
);

CREATE TABLE IF NOT EXISTS pool_accounts (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    deposited_usd REAL NOT NULL DEFAULT 0,
    cash_usd REAL NOT NULL DEFAULT 0,
    reserved_usd REAL NOT NULL DEFAULT 0,     -- intent budgets + open stake margin
    status TEXT NOT NULL DEFAULT 'active',    -- active | frozen
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    kind TEXT NOT NULL,        -- deposit | withdrawal | reserve | release |
                               -- trade_open | partial_exit | trade_close | adjustment
    amount_usd REAL NOT NULL,  -- cash delta for cash kinds; reserve delta for reserve kinds
    cash_after REAL NOT NULL,
    reserved_after REAL NOT NULL,
    ref TEXT,                  -- idempotency / audit key (trade id, order id, intent ref)
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS pool_events_dedupe
    ON pool_events (telegram_id, kind, ref)
    WHERE ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS pool_deposit_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    amount_usd REAL NOT NULL,
    txid TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | credited | denied
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER
);

-- One on-chain transfer credits exactly once. The deposit address is shared
-- with the yield sleeve's wallet, so the txid is the only thing that tells a
-- tester's transfer apart from house capital sitting in the same place --
-- which makes double-crediting one hash the way a tester's balance silently
-- becomes someone else's money. Denied requests are excluded so a hash can be
-- re-filed after a typo'd amount.
CREATE UNIQUE INDEX IF NOT EXISTS pool_deposit_txid_once
    ON pool_deposit_requests (txid)
    WHERE txid IS NOT NULL AND status != 'denied';

-- The address a tester funds FROM, and is therefore paid back TO. It does two
-- jobs: it is how an arriving transfer is attributed to a person without
-- trusting what they typed, and it is the only destination a payout may go to
-- (return-to-source), which keeps us out of the business of sending client
-- money to addresses nobody has proven they control.
CREATE TABLE IF NOT EXISTS pool_wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    address TEXT NOT NULL,                  -- lowercase, 0x-prefixed, 40 hex
    status TEXT NOT NULL DEFAULT 'pending', -- pending | verified | requested
                                            -- | replaced | rejected
    registered_at TEXT NOT NULL,
    verified_at TEXT,
    verified_txid TEXT,                     -- the transfer that proved control
    payouts_blocked_until TEXT,             -- set on an admin-approved change
    replaced_at TEXT,
    decided_by INTEGER,
    note TEXT
);
-- One live address per account, so "where do we pay this person" never has
-- two answers. 'requested' is excluded: a pending change coexists with the
-- address it wants to replace until an admin rules on it.
CREATE UNIQUE INDEX IF NOT EXISTS pool_wallets_active_once
    ON pool_wallets (telegram_id)
    WHERE status IN ('pending', 'verified');
-- And one account per address. Two testers claiming one address would make
-- sender-based attribution ambiguous, which is the entire point of holding it.
CREATE UNIQUE INDEX IF NOT EXISTS pool_wallets_address_once
    ON pool_wallets (address)
    WHERE status IN ('pending', 'verified');

CREATE TABLE IF NOT EXISTS pool_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL,          -- offer/cycle id (HQ) or mill_<idea_id> (mill)
    telegram_id INTEGER NOT NULL,
    risk_usd REAL NOT NULL,     -- budget reserved at Accept
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | pooled | missed | released
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS pool_intents_once
    ON pool_intents (ref, telegram_id);

CREATE TABLE IF NOT EXISTS pool_stakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    live_trade_id INTEGER NOT NULL,
    telegram_id INTEGER NOT NULL,
    share_frac REAL NOT NULL,       -- fraction of the aggregate position
    qty REAL NOT NULL,              -- share_frac × fill qty, for display
    cost_usd REAL NOT NULL,         -- margin reserved (share of fill notional)
    released_usd REAL NOT NULL DEFAULT 0,  -- margin given back as legs close
    risk_usd REAL NOT NULL,         -- dollars at the armed stop
    realized_pnl_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',    -- open | closed
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS pool_stakes_once
    ON pool_stakes (live_trade_id, telegram_id);

CREATE TABLE IF NOT EXISTS pool_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_FROZEN_KEY = "intents_frozen"
_RECON_KEY = "last_reconcile"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def init_db() -> None:
    with _connect():
        pass


def get_meta(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM pool_meta WHERE key = ?", (key,)
        ).fetchone()
    return str(row["value"]) if row else None


def set_meta(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO pool_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# Access — Admit/Deny into the product
# ---------------------------------------------------------------------------

def request_access(telegram_id: int, username: str | None = None) -> str:
    """Record that a user wants in. Returns the row's status.

    'new' means an admin card should go out now; every other status means one
    already did (or the decision is made), so the admin is not re-pinged on
    every message the user sends while waiting.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM approved_users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if row is not None:
            if username:
                conn.execute(
                    "UPDATE approved_users SET username = COALESCE(?, username) "
                    "WHERE telegram_id = ?",
                    (username, telegram_id),
                )
            return str(row["status"])
        conn.execute(
            "INSERT INTO approved_users (telegram_id, username, status, requested_at) "
            "VALUES (?, ?, 'pending', ?)",
            (telegram_id, username, _now()),
        )
    return "new"


def approve_user(telegram_id: int, *, admin_id: int, username: str | None = None) -> bool:
    """Admit a user to the product; opens a zero-balance pool account."""
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO approved_users (telegram_id, username, status, requested_at, "
            "decided_at, decided_by) VALUES (?, ?, 'approved', ?, ?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET status = 'approved', "
            "decided_at = excluded.decided_at, decided_by = excluded.decided_by, "
            "username = COALESCE(excluded.username, approved_users.username)",
            (telegram_id, username, now, now, admin_id),
        )
        conn.execute(
            "INSERT INTO pool_accounts (telegram_id, username, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET "
            "username = COALESCE(excluded.username, pool_accounts.username)",
            (telegram_id, username, now),
        )
    logger.info("pool: user %s admitted by %s", telegram_id, admin_id)
    return True


def deny_user(telegram_id: int, *, admin_id: int) -> bool:
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO approved_users (telegram_id, status, requested_at, "
            "decided_at, decided_by) VALUES (?, 'denied', ?, ?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET status = 'denied', "
            "decided_at = excluded.decided_at, decided_by = excluded.decided_by",
            (telegram_id, now, now, admin_id),
        )
    logger.info("pool: user %s denied by %s", telegram_id, admin_id)
    return True


def is_approved(telegram_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM approved_users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return bool(row and str(row["status"]) == "approved")


def access_status(telegram_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM approved_users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return str(row["status"]) if row else None


def admin_ids() -> list[int]:
    """Who may Admit users, credit deposits, and run /credit //debit.

    Env and code config are merged so an operator can be added without a
    deploy. Falls back to the internal allowlist and then the admin chat,
    because an empty list means Admit cards have nowhere to go and nobody
    can ever be let into the product.
    """
    ids = {int(i) for i in bot_config.POOL_ADMIN_TELEGRAM_IDS}
    ids.update(int(i) for i in getattr(config, "POOL_ADMIN_TELEGRAM_IDS", []))
    if ids:
        return sorted(ids)
    if config.INTERNAL_TELEGRAM_IDS:
        return [int(i) for i in config.INTERNAL_TELEGRAM_IDS]
    admin = config.TELEGRAM_ADMIN_CHAT_ID or config.TELEGRAM_CHAT_ID
    if admin:
        try:
            return [int(str(admin).strip())]
        except ValueError:
            return []
    return []


def is_admin(telegram_id: int) -> bool:
    return int(telegram_id) in admin_ids()


# ---------------------------------------------------------------------------
# Accounts and the event journal — the ONLY cash/reserve writers
# ---------------------------------------------------------------------------

def get_account(telegram_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pool_accounts WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return dict(row) if row else None


def is_funded(telegram_id: int) -> bool:
    account = get_account(telegram_id)
    return bool(account and float(account["cash_usd"]) > 0)


def prospective_accept(
    telegram_id: int,
    *,
    entry: float,
    stop_loss: float,
) -> dict[str, Any]:
    """What Accept would reserve right now — card copy only, no journal write.

    Risk = ``POOL_RISK_PCT`` × available cash. Position size is the notional
    that risks exactly that many dollars at this stop
    (``risk × entry / |entry − stop|``). Real share at fill can be smaller if
    they dilute into a house clip that did not grow an extra contract.
    """
    account = get_account(telegram_id)
    if account is None or float(account["cash_usd"]) <= 0:
        return {"ok": False, "reason": "not_funded"}
    cash = float(account["cash_usd"])
    reserved = float(account["reserved_usd"])
    available = cash - reserved
    if cash < float(bot_config.POOL_MIN_EQUITY_USD):
        return {
            "ok": False,
            "reason": "below_min_equity",
            "minimum_usd": float(bot_config.POOL_MIN_EQUITY_USD),
            "cash_usd": cash,
        }
    if available <= 0:
        return {"ok": False, "reason": "no_available_cash", "cash_usd": cash}
    risk_pct = float(bot_config.POOL_RISK_PCT)
    risk = round(available * risk_pct, 2)
    risk_per_unit = abs(float(entry) - float(stop_loss))
    notional = (
        round(risk * float(entry) / risk_per_unit, 2) if risk_per_unit > 0 else 0.0
    )
    return {
        "ok": True,
        "cash_usd": cash,
        "available_usd": round(available, 2),
        "risk_usd": risk,
        "risk_pct": risk_pct,
        "notional_usd": notional,
    }


def list_accounts() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_accounts ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def _apply_event(
    conn: sqlite3.Connection,
    telegram_id: int,
    *,
    kind: str,
    amount_usd: float,
    ref: str | None,
    note: str | None = None,
) -> bool:
    """Append one journal row and move the balances it describes.

    Cash kinds (deposit/withdrawal/partial_exit/trade_close/adjustment) move
    ``cash_usd`` by ``amount_usd``. Reserve kinds (reserve/trade_open move it
    up, release moves it down) move ``reserved_usd``. Returns False when the
    (telegram_id, kind, ref) key already exists — the caller treats that as
    "already booked", never as an error.
    """
    account = conn.execute(
        "SELECT cash_usd, reserved_usd, deposited_usd FROM pool_accounts "
        "WHERE telegram_id = ?",
        (telegram_id,),
    ).fetchone()
    if account is None:
        raise ValueError(f"no pool account for {telegram_id}")

    cash = float(account["cash_usd"])
    reserved = float(account["reserved_usd"])
    deposited = float(account["deposited_usd"])

    if kind in ("reserve", "trade_open"):
        reserved += amount_usd
    elif kind == "release":
        reserved = max(reserved - amount_usd, 0.0)
    else:
        cash += amount_usd
        if kind == "deposit":
            deposited += amount_usd
        elif kind == "withdrawal":
            deposited = max(deposited + amount_usd, 0.0)  # amount is negative

    try:
        conn.execute(
            "INSERT INTO pool_events (telegram_id, kind, amount_usd, cash_after, "
            "reserved_after, ref, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (telegram_id, kind, amount_usd, cash, reserved, ref, note, _now()),
        )
    except sqlite3.IntegrityError:
        return False  # same (id, kind, ref) already journaled — idempotent no-op

    conn.execute(
        "UPDATE pool_accounts SET cash_usd = ?, reserved_usd = ?, deposited_usd = ? "
        "WHERE telegram_id = ?",
        (cash, reserved, deposited, telegram_id),
    )
    return True


def credit(
    telegram_id: int,
    amount_usd: float,
    *,
    admin_id: int,
    note: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    """Admin books a deposit that has landed on the venue."""
    if amount_usd <= 0:
        return {"ok": False, "reason": "bad_amount"}
    with _connect() as conn:
        try:
            done = _apply_event(
                conn, telegram_id, kind="deposit", amount_usd=float(amount_usd),
                ref=ref, note=note or f"credited by {admin_id}",
            )
        except ValueError:
            return {"ok": False, "reason": "no_account"}
    if not done:
        return {"ok": False, "reason": "duplicate"}
    account = get_account(telegram_id) or {}
    logger.info("pool: credited %s $%.2f by %s", telegram_id, amount_usd, admin_id)
    return {"ok": True, "cash_usd": account.get("cash_usd")}


def debit(
    telegram_id: int,
    amount_usd: float,
    *,
    admin_id: int,
    note: str | None = None,
) -> dict[str, Any]:
    """Admin books a withdrawal / correction. Cannot touch reserved margin."""
    if amount_usd <= 0:
        return {"ok": False, "reason": "bad_amount"}
    with _connect() as conn:
        account = conn.execute(
            "SELECT cash_usd, reserved_usd FROM pool_accounts WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if account is None:
            return {"ok": False, "reason": "no_account"}
        available = float(account["cash_usd"]) - float(account["reserved_usd"])
        if amount_usd > available + 1e-9:
            return {"ok": False, "reason": "insufficient_available",
                    "available_usd": round(available, 2)}
        _apply_event(
            conn, telegram_id, kind="withdrawal", amount_usd=-float(amount_usd),
            ref=None, note=note or f"debited by {admin_id}",
        )
    account2 = get_account(telegram_id) or {}
    logger.info("pool: debited %s $%.2f by %s", telegram_id, amount_usd, admin_id)
    return {"ok": True, "cash_usd": account2.get("cash_usd")}


# ---------------------------------------------------------------------------
# Wallet registration — where a tester's money comes from, and goes back to
# ---------------------------------------------------------------------------

def normalize_address(address: str | None) -> str | None:
    """Lower-cased, 0x-prefixed 40-hex address, or None if it is not one.

    Deliberately **no EIP-55 checksum check.** There is no keccak-256 in this
    environment (`hashlib.sha3_256` is the NIST variant, not keccak), and
    hand-rolling one in a money path would trade a rare failure for a worse
    one: a subtly wrong implementation rejects addresses that are fine, or
    blesses ones that are not. The property that actually matters is proven
    elsewhere and more strongly — a deposit arriving *from* this address is
    cryptographic evidence the tester controls it, which no checksum can give.
    Until that arrives the address is `pending` and no payout may use it.

    Case is dropped because the same address arrives checksummed from one
    wallet and lowercase from another, and two spellings of one address would
    defeat the indexes that keep it to a single owner.
    """
    if address is None:
        return None
    raw = str(address).strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 40 or any(c not in "0123456789abcdef" for c in raw):
        return None
    return f"0x{raw}"


def get_wallet(telegram_id: int) -> dict[str, Any] | None:
    """The tester's live address, verified or merely registered."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pool_wallets WHERE telegram_id = ? AND status IN "
            "('pending', 'verified') ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        ).fetchone()
    return dict(row) if row else None


def get_wallet_change_request(telegram_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pool_wallets WHERE telegram_id = ? AND "
            "status = 'requested' ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        ).fetchone()
    return dict(row) if row else None


def wallet_owner(address: str) -> int | None:
    """Which tester an arriving transfer belongs to, by its sender."""
    clean = normalize_address(address)
    if clean is None:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT telegram_id FROM pool_wallets WHERE address = ? AND "
            "status IN ('pending', 'verified')",
            (clean,),
        ).fetchone()
    return int(row["telegram_id"]) if row else None


def register_wallet(telegram_id: int, address: str) -> dict[str, Any]:
    """Bind the address a tester funds from and will be paid back to.

    A **first** registration is self-serve. There is nothing to steal yet: the
    account holds no money, and the address cannot receive a payout until a
    deposit arrives from it. Gating it on an admin would only add a round-trip
    between someone deciding to fund and being able to.

    **Replacing** one is the dangerous operation, and goes through
    `request_wallet_change` instead. Whoever holds the Telegram account can
    ask to be paid somewhere new, so a takeover's first move is to re-point
    the payout address — which is why that path needs a human and a cooldown.
    """
    if not is_approved(telegram_id):
        return {"ok": False, "reason": "not_approved"}
    clean = normalize_address(address)
    if clean is None:
        return {"ok": False, "reason": "malformed"}

    current = get_wallet(telegram_id)
    if current is not None:
        if str(current["address"]) == clean:
            return {"ok": True, "unchanged": True, "wallet": current}
        return {"ok": False, "reason": "change_needs_admin",
                "current": str(current["address"])}

    owner = wallet_owner(clean)
    if owner is not None and int(owner) != int(telegram_id):
        # Not named, deliberately: whether a given address is already in the
        # book is not this user's information to learn.
        logger.warning(
            "pool: %s tried to register %s, held by %s", telegram_id, clean, owner
        )
        return {"ok": False, "reason": "address_taken"}

    with _connect() as conn:
        conn.execute(
            "INSERT INTO pool_wallets (telegram_id, address, status, registered_at) "
            "VALUES (?, ?, 'pending', ?)",
            (telegram_id, clean, _now()),
        )
    logger.info("pool: %s registered wallet %s (pending)", telegram_id, clean)
    return {"ok": True, "address": clean, "status": "pending"}


def request_wallet_change(telegram_id: int, address: str) -> dict[str, Any]:
    """File a payout-address change for an admin to rule on."""
    if not is_approved(telegram_id):
        return {"ok": False, "reason": "not_approved"}
    clean = normalize_address(address)
    if clean is None:
        return {"ok": False, "reason": "malformed"}

    current = get_wallet(telegram_id)
    if current is None:
        return register_wallet(telegram_id, clean)
    if str(current["address"]) == clean:
        return {"ok": False, "reason": "same_address"}
    if get_wallet_change_request(telegram_id) is not None:
        return {"ok": False, "reason": "already_pending"}
    if (owner := wallet_owner(clean)) is not None and int(owner) != int(telegram_id):
        return {"ok": False, "reason": "address_taken"}

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO pool_wallets (telegram_id, address, status, registered_at, "
            "note) VALUES (?, ?, 'requested', ?, ?)",
            (telegram_id, clean, _now(), f"replaces {current['address']}"),
        )
    logger.info(
        "pool: %s asked to move payouts from %s to %s",
        telegram_id, current["address"], clean,
    )
    return {"ok": True, "request_id": int(cur.lastrowid or 0), "address": clean,
            "previous": str(current["address"]),
            "cooldown_hours": float(bot_config.POOL_WALLET_COOLDOWN_HOURS)}


def decide_wallet_change(
    request_id: int, *, admin_id: int, approve: bool
) -> dict[str, Any]:
    """Approve or refuse a payout-address change.

    An approved change starts a payout cooldown on the new address. The point
    is not to slow the honest case down for its own sake — it is that if this
    change was not the tester's doing, there is a window in which they can
    still say so before their money leaves.
    """
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pool_wallets WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if str(row["status"]) != "requested":
            return {"ok": False, "reason": "already_decided",
                    "status": str(row["status"])}

        telegram_id = int(row["telegram_id"])
        if not approve:
            conn.execute(
                "UPDATE pool_wallets SET status = 'rejected', decided_by = ?, "
                "replaced_at = ? WHERE id = ?",
                (admin_id, now, request_id),
            )
            return {"ok": True, "approved": False, "telegram_id": telegram_id,
                    "address": str(row["address"])}

        blocked_until = (
            datetime.now(timezone.utc)
            + timedelta(hours=float(bot_config.POOL_WALLET_COOLDOWN_HOURS))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Retire the old row first: both it and the incoming one would
        # otherwise be 'verified'/'pending' at once, which the one-live-address
        # index exists to make impossible.
        conn.execute(
            "UPDATE pool_wallets SET status = 'replaced', replaced_at = ? "
            "WHERE telegram_id = ? AND status IN ('pending', 'verified')",
            (now, telegram_id),
        )
        conn.execute(
            "UPDATE pool_wallets SET status = 'pending', decided_by = ?, "
            "payouts_blocked_until = ? WHERE id = ?",
            (admin_id, blocked_until, request_id),
        )
    logger.info(
        "pool: %s payout address moved to %s by %s, payouts held until %s",
        telegram_id, row["address"], admin_id, blocked_until,
    )
    return {"ok": True, "approved": True, "telegram_id": telegram_id,
            "address": str(row["address"]),
            "payouts_blocked_until": blocked_until}


def mark_wallet_verified(
    address: str, *, txid: str | None = None
) -> dict[str, Any]:
    """Record that a deposit arrived from this address, proving control.

    Called by the deposit path rather than by a user, because the whole value
    of the flag is that a tester cannot set it themselves.
    """
    clean = normalize_address(address)
    if clean is None:
        return {"ok": False, "reason": "malformed"}
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pool_wallets WHERE address = ? AND status IN "
            "('pending', 'verified')",
            (clean,),
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_registered"}
        if str(row["status"]) == "verified":
            return {"ok": True, "already": True,
                    "telegram_id": int(row["telegram_id"])}
        conn.execute(
            "UPDATE pool_wallets SET status = 'verified', verified_at = ?, "
            "verified_txid = COALESCE(?, verified_txid) WHERE id = ?",
            (_now(), normalize_txid(txid), int(row["id"])),
        )
    logger.info("pool: wallet %s verified by an inbound transfer", clean)
    return {"ok": True, "telegram_id": int(row["telegram_id"]), "address": clean}


def payout_target(telegram_id: int) -> dict[str, Any]:
    """Where a withdrawal may go, or why it may not go anywhere yet.

    The withdrawal path is not built; this is the single place that decides
    the destination so that when it is, there is one answer rather than a
    second opinion written next to it.
    """
    wallet = get_wallet(telegram_id)
    if wallet is None:
        return {"ok": False, "reason": "no_wallet"}
    if str(wallet["status"]) != "verified":
        # Paying an unproven address would mean sending client funds somewhere
        # on nothing but a typed claim.
        return {"ok": False, "reason": "unverified",
                "address": str(wallet["address"])}
    held = wallet["payouts_blocked_until"]
    if held and str(held) > _now():
        return {"ok": False, "reason": "cooldown", "until": str(held),
                "address": str(wallet["address"])}
    return {"ok": True, "address": str(wallet["address"])}


# ---------------------------------------------------------------------------
# Deposit requests — user asks, admin credits with one tap
# ---------------------------------------------------------------------------

def normalize_txid(txid: str | None) -> str | None:
    """Lower-cased, 0x-prefixed hash, or None if it is not one.

    Case and a missing prefix are the two ways the same transfer arrives
    looking like two different ones, which would defeat the uniqueness index
    that stops a hash being credited twice.
    """
    if txid is None:
        return None
    raw = str(txid).strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
        return None
    return f"0x{raw}"


def request_deposit(
    telegram_id: int, amount_usd: float, *, txid: str | None = None
) -> dict[str, Any]:
    """File a deposit claim for an admin to verify against the chain.

    Two things are required whenever a deposit address is configured, and they
    do different jobs. A **registered wallet** is who the money is from: it is
    what lets an arriving transfer be attributed to a person by its sender
    rather than by what someone typed, and it is the address any payout must
    later return to. A **txid** is which transfer it was, and is what stops
    one deposit being credited twice or claimed by the wrong account.
    """
    if not is_approved(telegram_id):
        return {"ok": False, "reason": "not_approved"}
    if amount_usd < float(bot_config.POOL_MIN_DEPOSIT_USD):
        return {"ok": False, "reason": "below_minimum",
                "minimum_usd": float(bot_config.POOL_MIN_DEPOSIT_USD)}

    clean = normalize_txid(txid)
    if config.POOL_DEPOSIT_ADDRESS:
        wallet = get_wallet(telegram_id)
        if wallet is None:
            return {"ok": False, "reason": "wallet_required"}
        if txid is None:
            return {"ok": False, "reason": "txid_required"}
        if clean is None:
            return {"ok": False, "reason": "txid_malformed"}

    with _connect() as conn:
        pending = conn.execute(
            "SELECT id FROM pool_deposit_requests WHERE telegram_id = ? "
            "AND status = 'pending'",
            (telegram_id,),
        ).fetchone()
        if pending is not None:
            return {"ok": False, "reason": "already_pending",
                    "request_id": int(pending["id"])}
        if clean is not None:
            seen = conn.execute(
                "SELECT id, telegram_id, status FROM pool_deposit_requests "
                "WHERE txid = ? AND status != 'denied'",
                (clean,),
            ).fetchone()
            if seen is not None:
                logger.warning(
                    "pool: %s re-filed txid %s already on request #%s",
                    telegram_id, clean, seen["id"],
                )
                return {"ok": False, "reason": "txid_already_claimed",
                        "request_id": int(seen["id"]),
                        "claimed_by": int(seen["telegram_id"])}
        cur = conn.execute(
            "INSERT INTO pool_deposit_requests (telegram_id, amount_usd, txid, "
            "created_at) VALUES (?, ?, ?, ?)",
            (telegram_id, float(amount_usd), clean, _now()),
        )
    return {"ok": True, "request_id": int(cur.lastrowid or 0), "txid": clean}


def pending_inbound_usd() -> float:
    """Claimed-but-uncredited deposits — venue cash that is already not ours.

    Deposits land straight in the Coinbase spot wallet, so the reconciler
    counts them from the moment they arrive, and until an admin credits them
    they inflate `house_residual_usd`. This is that overstatement: the slice
    of apparent house money that is really a tester's. Nothing spends against
    it; it exists so the gap is visible rather than inferred.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM "
            "pool_deposit_requests WHERE status = 'pending'"
        ).fetchone()
    return float(row["total"] or 0.0)


def get_deposit_request(request_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pool_deposit_requests WHERE id = ?", (request_id,)
        ).fetchone()
    return dict(row) if row else None


def decide_deposit(
    request_id: int, *, admin_id: int, approve: bool, sender: str | None = None
) -> dict[str, Any]:
    """One admin tap: credit the request's amount, or deny it.

    ``sender`` is the address the transfer actually came from. Passing it is
    what verifies the tester's registered wallet, so it is a parameter rather
    than an assumption: tapping Credit means an admin saw funds arrive, which
    is not the same as having seen *where they came from*. Marking a wallet
    verified on the strength of the tap alone would manufacture a proof we do
    not hold, and that proof is the only thing standing between a payout and
    an address nobody has shown they control. The chain watcher will pass it;
    until then wallets stay `pending`, which costs nothing while there is no
    withdrawal path.
    """
    req = get_deposit_request(request_id)
    if req is None:
        return {"ok": False, "reason": "not_found"}
    if str(req["status"]) != "pending":
        return {"ok": False, "reason": "already_decided", "status": req["status"]}

    status = "credited" if approve else "denied"
    with _connect() as conn:
        conn.execute(
            "UPDATE pool_deposit_requests SET status = ?, decided_at = ?, "
            "decided_by = ? WHERE id = ? AND status = 'pending'",
            (status, _now(), admin_id, request_id),
        )
    if not approve:
        return {"ok": True, "status": "denied", "telegram_id": req["telegram_id"],
                "amount_usd": req["amount_usd"]}

    verified = False
    if sender is not None:
        registered = get_wallet(int(req["telegram_id"]))
        clean = normalize_address(sender)
        if registered and clean == str(registered["address"]):
            verified = bool(
                mark_wallet_verified(clean, txid=req["txid"]).get("ok")
            )
        else:
            # Money arrived and is credited either way — it is in the account.
            # But it did not come from where this tester said, so the address
            # stays unproven and a payout must not use it.
            logger.warning(
                "pool: deposit #%s for %s came from %s, registered %s — wallet "
                "NOT verified",
                request_id, req["telegram_id"], clean,
                registered and registered["address"],
            )

    result = credit(
        int(req["telegram_id"]),
        float(req["amount_usd"]),
        admin_id=admin_id,
        ref=f"deposit_request:{request_id}",
        note=f"deposit request #{request_id}",
    )
    result.update(
        {"status": "credited", "telegram_id": req["telegram_id"],
         "amount_usd": req["amount_usd"], "wallet_verified": verified}
    )
    return result


# ---------------------------------------------------------------------------
# Intents — the pre-fill Accept queue
# ---------------------------------------------------------------------------

def intents_frozen() -> str | None:
    """Reason string when new intents are frozen (reconcile drift), else None."""
    raw = get_meta(_FROZEN_KEY)
    return raw if raw else None


def freeze_intents(reason: str) -> None:
    set_meta(_FROZEN_KEY, reason)
    logger.error("pool: NEW INTENTS FROZEN — %s", reason)


def unfreeze_intents() -> None:
    set_meta(_FROZEN_KEY, "")
    logger.info("pool: intents unfrozen")


def record_intent(ref: str, telegram_id: int) -> dict[str, Any]:
    """A funded tester's Accept: reserve their risk budget against this ref.

    The budget is POOL_RISK_PCT of *available* cash (cash minus everything
    already reserved), so ten concurrent Accepts cannot promise the same
    dollars twice.
    """
    frozen = intents_frozen()
    if frozen:
        return {"ok": False, "reason": "frozen", "detail": frozen}
    if not is_approved(telegram_id):
        return {"ok": False, "reason": "not_approved"}
    account = get_account(telegram_id)
    if account is None or float(account["cash_usd"]) <= 0:
        return {"ok": False, "reason": "not_funded"}

    cash = float(account["cash_usd"])
    reserved = float(account["reserved_usd"])
    available = cash - reserved
    if cash < float(bot_config.POOL_MIN_EQUITY_USD):
        return {"ok": False, "reason": "below_min_equity",
                "minimum_usd": float(bot_config.POOL_MIN_EQUITY_USD)}
    risk = round(available * float(bot_config.POOL_RISK_PCT), 2)
    if risk <= 0:
        return {"ok": False, "reason": "no_available_cash"}

    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO pool_intents (ref, telegram_id, risk_usd, created_at) "
                "VALUES (?, ?, ?, ?)",
                (ref, telegram_id, risk, _now()),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT status, risk_usd FROM pool_intents WHERE ref = ? "
                "AND telegram_id = ?",
                (ref, telegram_id),
            ).fetchone()
            return {"ok": False, "reason": "already_recorded",
                    "status": row["status"] if row else None}
        _apply_event(
            conn, telegram_id, kind="reserve", amount_usd=risk,
            ref=f"intent:{ref}", note="pool intent",
        )
    logger.info("pool: intent %s user %s risk $%.2f", ref, telegram_id, risk)
    return {"ok": True, "risk_usd": risk}


def pending_intents(ref: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_intents WHERE ref = ? AND status = 'pending' "
            "ORDER BY created_at ASC",
            (ref,),
        ).fetchall()
    return [dict(r) for r in rows]


def _finish_intent(
    conn: sqlite3.Connection, intent: dict[str, Any], status: str
) -> None:
    """Terminal-state an intent and give its reserve back."""
    conn.execute(
        "UPDATE pool_intents SET status = ?, decided_at = ? WHERE id = ?",
        (status, _now(), int(intent["id"])),
    )
    _apply_event(
        conn,
        int(intent["telegram_id"]),
        kind="release",
        amount_usd=float(intent["risk_usd"]),
        ref=f"intent:{intent['ref']}:{status}",
        note=f"intent {status}",
    )


def release_intents(ref: str, *, status: str = "missed") -> list[dict[str, Any]]:
    """Terminal-state every pending intent on a ref (order never fired, was
    replaced, expired, or the fill was rejected). Returns the released rows so
    the caller can DM each tester."""
    released: list[dict[str, Any]] = []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_intents WHERE ref = ? AND status = 'pending'",
            (ref,),
        ).fetchall()
        for row in rows:
            intent = dict(row)
            _finish_intent(conn, intent, status)
            released.append(intent)
    return released


def expire_stale_intents(active_refs: set[str]) -> list[dict[str, Any]]:
    """Release pending intents whose ref no longer has a live order path.

    Called from the watchdog with the set of refs that can still fire (waiting
    live_pending cycle ids + open mill ideas). Anything else is a promise that
    can no longer be kept, so the reserve goes back and the tester is told.
    """
    released: list[dict[str, Any]] = []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_intents WHERE status = 'pending'"
        ).fetchall()
        for row in rows:
            intent = dict(row)
            if str(intent["ref"]) in active_refs:
                continue
            _finish_intent(conn, intent, "missed")
            released.append(intent)
    return released


# ---------------------------------------------------------------------------
# Fill-time sizing and the pro-rata split
# ---------------------------------------------------------------------------

def extra_contracts_for(
    ref: str, *, risk_per_unit: float, floor: float | None
) -> tuple[float, list[dict[str, Any]]]:
    """How much size pending tester budgets add to the house order.

    Whole contracts only — the venue cannot fill a fraction of a nano. The
    remainder of the budgets still buys ownership of the aggregate fill via
    the pro-rata split; it just does not move the venue order.
    """
    intents = pending_intents(ref)
    if not intents or not floor or floor <= 0 or risk_per_unit <= 0:
        return 0.0, intents
    total_budget = sum(float(i["risk_usd"]) for i in intents)
    per_contract_risk = risk_per_unit * floor
    contracts = int(total_budget / per_contract_risk)
    return contracts * floor, intents


def open_stakes(
    live_trade_id: int,
    ref: str,
    *,
    fill_qty: float,
    fill_price: float,
    risk_per_unit: float,
    house_risk_usd: float,
) -> list[dict[str, Any]]:
    """Split the actual fill pro-rata to budgets and open one stake per tester.

    House budget participates in the split, so the house holds the residual
    share (1 − sum of tester fractions) implicitly. A tester whose margin
    (share of fill notional) exceeds their available cash is trimmed to what
    they can cover; a tester who can cover nothing is released, not opened.
    """
    intents = pending_intents(ref)
    if not intents or fill_qty <= 0 or fill_price <= 0:
        return []

    total_budget = float(house_risk_usd) + sum(float(i["risk_usd"]) for i in intents)
    if total_budget <= 0:
        return []
    notional = fill_qty * fill_price

    opened: list[dict[str, Any]] = []
    with _connect() as conn:
        for intent in intents:
            intent = dict(intent)
            uid = int(intent["telegram_id"])
            budget = float(intent["risk_usd"])
            share = budget / total_budget

            account = conn.execute(
                "SELECT cash_usd, reserved_usd FROM pool_accounts "
                "WHERE telegram_id = ?",
                (uid,),
            ).fetchone()
            if account is None:
                _finish_intent(conn, intent, "released")
                continue
            # Their intent reserve is inside reserved_usd; it is swapped for
            # the stake margin below, so it does not count against them here.
            available = (
                float(account["cash_usd"])
                - float(account["reserved_usd"])
                + budget
            )
            cost = share * notional
            if cost > available + 1e-9:
                if available <= 0:
                    _finish_intent(conn, intent, "released")
                    continue
                share = available / notional
                cost = available
            qty_share = share * fill_qty
            risk_actual = round(risk_per_unit * qty_share, 2)

            conn.execute(
                "UPDATE pool_intents SET status = 'pooled', decided_at = ? "
                "WHERE id = ?",
                (_now(), int(intent["id"])),
            )
            # Swap the intent's risk reserve for the stake's margin reserve.
            _apply_event(
                conn, uid, kind="release", amount_usd=budget,
                ref=f"intent:{ref}:pooled", note="intent pooled",
            )
            _apply_event(
                conn, uid, kind="trade_open", amount_usd=round(cost, 2),
                ref=f"trade:{live_trade_id}:open", note=f"stake in trade #{live_trade_id}",
            )
            conn.execute(
                "INSERT INTO pool_stakes (live_trade_id, telegram_id, share_frac, "
                "qty, cost_usd, risk_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(live_trade_id, telegram_id) DO NOTHING",
                (
                    live_trade_id, uid, round(share, 8), round(qty_share, 8),
                    round(cost, 2), risk_actual, _now(),
                ),
            )
            opened.append(
                {
                    "telegram_id": uid,
                    "share_frac": share,
                    "qty": qty_share,
                    "cost_usd": cost,
                    "risk_usd": risk_actual,
                }
            )
    logger.info(
        "pool: trade #%s opened %d tester stake(s) on ref %s",
        live_trade_id, len(opened), ref,
    )
    return opened


def open_stakes_for(trade_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_stakes WHERE live_trade_id = ? AND status = 'open'",
            (trade_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def stakes_for(trade_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_stakes WHERE live_trade_id = ?", (trade_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Exit booking — pro-rata, idempotent by venue order id
# ---------------------------------------------------------------------------

def book_exit(
    trade_id: int,
    *,
    exit_qty: float,
    exit_price: float,
    pnl_usd: float,
    order_id: str,
    reason: str,
    qty_total: float,
) -> list[dict[str, Any]]:
    """Credit every open stake its share of one booked exit leg.

    Mirrors live_ledger.record_partial_exit and is idempotent the same way:
    the venue order id keys the journal row, so re-sweeping a settled order
    books nothing twice. ``qty_total`` is the trade's original size — the
    fraction of margin released follows the fraction of the position closed.
    """
    stakes = open_stakes_for(trade_id)
    if not stakes:
        return []
    frac_closed = min(exit_qty / qty_total, 1.0) if qty_total > 0 else 1.0

    booked: list[dict[str, Any]] = []
    with _connect() as conn:
        for stake in stakes:
            uid = int(stake["telegram_id"])
            share = float(stake["share_frac"])
            pnl_share = round(pnl_usd * share, 2)
            done = _apply_event(
                conn, uid, kind="partial_exit", amount_usd=pnl_share,
                ref=f"trade:{trade_id}:exit:{order_id}",
                note=f"{reason} @ {exit_price:.2f}",
            )
            if not done:
                continue  # this leg was already booked for this tester
            remaining = float(stake["cost_usd"]) - float(stake["released_usd"])
            release = round(min(float(stake["cost_usd"]) * frac_closed, remaining), 2)
            if release > 0:
                _apply_event(
                    conn, uid, kind="release", amount_usd=release,
                    ref=f"trade:{trade_id}:release:{order_id}",
                    note="margin released",
                )
            conn.execute(
                "UPDATE pool_stakes SET realized_pnl_usd = realized_pnl_usd + ?, "
                "released_usd = released_usd + ? WHERE id = ?",
                (pnl_share, release, int(stake["id"])),
            )
            booked.append(
                {
                    "telegram_id": uid,
                    "pnl_usd": pnl_share,
                    "reason": reason,
                    "exit_price": exit_price,
                }
            )
    return booked


def book_close(trade_id: int, *, close_reason: str) -> list[dict[str, Any]]:
    """Finalise every stake on a closed trade: release leftover margin.

    All P&L flowed through book_exit already (record_close carries no new
    leg), so this only returns any margin the leg-by-leg release rounding
    left behind and marks the stakes closed.
    """
    closed: list[dict[str, Any]] = []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_stakes WHERE live_trade_id = ? AND status = 'open'",
            (trade_id,),
        ).fetchall()
        for row in rows:
            stake = dict(row)
            uid = int(stake["telegram_id"])
            remaining = round(
                float(stake["cost_usd"]) - float(stake["released_usd"]), 2
            )
            if remaining > 0:
                _apply_event(
                    conn, uid, kind="release", amount_usd=remaining,
                    ref=f"trade:{trade_id}:release:final",
                    note="margin released on close",
                )
            conn.execute(
                "UPDATE pool_stakes SET status = 'closed', closed_at = ?, "
                "released_usd = cost_usd WHERE id = ?",
                (_now(), int(stake["id"])),
            )
            stake["close_reason"] = close_reason
            closed.append(stake)
    return closed


# ---------------------------------------------------------------------------
# Portfolio and reconciliation
# ---------------------------------------------------------------------------

def portfolio(telegram_id: int, spots: dict[str, float] | None = None) -> dict[str, Any]:
    """Everything /portfolio shows: cash, open stakes MTM, realized, history."""
    account = get_account(telegram_id)
    if account is None:
        return {"ok": False, "reason": "no_account"}

    import live_ledger

    with _connect() as conn:
        stake_rows = conn.execute(
            "SELECT * FROM pool_stakes WHERE telegram_id = ? "
            "ORDER BY created_at DESC LIMIT 50",
            (telegram_id,),
        ).fetchall()
        event_rows = conn.execute(
            "SELECT * FROM pool_events WHERE telegram_id = ? AND kind IN "
            "('deposit', 'withdrawal', 'partial_exit', 'trade_close', 'adjustment') "
            "ORDER BY id DESC LIMIT 10",
            (telegram_id,),
        ).fetchall()

    open_stakes_out: list[dict[str, Any]] = []
    closed_stakes_out: list[dict[str, Any]] = []
    unrealized = 0.0
    for row in stake_rows:
        stake = dict(row)
        trade = live_ledger.get_trade(int(stake["live_trade_id"])) or {}
        product = str(trade.get("product_id") or "")
        item = {
            "trade_id": int(stake["live_trade_id"]),
            "product_id": product,
            "side": trade.get("side"),
            "entry": trade.get("entry"),
            "qty": float(stake["qty"]),
            "share_frac": float(stake["share_frac"]),
            "cost_usd": float(stake["cost_usd"]),
            "risk_usd": float(stake["risk_usd"]),
            "realized_pnl_usd": float(stake["realized_pnl_usd"]),
            "status": stake["status"],
        }
        if str(stake["status"]) == "open":
            mark = float((spots or {}).get(product) or 0.0)
            if mark > 0 and trade:
                direction = 1.0 if str(trade.get("side")) == "long" else -1.0
                qty_open_total = float(trade.get("qty_open") or 0.0)
                qty_open_share = qty_open_total * float(stake["share_frac"])
                item["unrealized_usd"] = round(
                    (mark - float(trade.get("entry") or 0.0))
                    * qty_open_share
                    * direction,
                    2,
                )
                unrealized += item["unrealized_usd"]
            open_stakes_out.append(item)
        else:
            closed_stakes_out.append(item)

    realized_total = sum(float(s["realized_pnl_usd"]) for s in map(dict, stake_rows))
    return {
        "ok": True,
        "cash_usd": float(account["cash_usd"]),
        "reserved_usd": float(account["reserved_usd"]),
        "available_usd": round(
            float(account["cash_usd"]) - float(account["reserved_usd"]), 2
        ),
        "deposited_usd": float(account["deposited_usd"]),
        "realized_pnl_usd": round(realized_total, 2),
        "unrealized_pnl_usd": round(unrealized, 2),
        "open_stakes": open_stakes_out,
        "closed_stakes": closed_stakes_out[:5],
        "events": [dict(r) for r in event_rows],
        "frozen": intents_frozen(),
    }


def total_tester_cash() -> float:
    with _connect() as conn:
        row = conn.execute("SELECT SUM(cash_usd) AS s FROM pool_accounts").fetchone()
    return float(row["s"] or 0.0)


def reconcile(
    venue_assets_usd: float,
    *,
    breakdown: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The fiduciary floor check: the venue must cover every tester's claim.

    Testers' realized cash is their claim on the account. If the venue's
    assets cannot cover it (beyond tolerance), something is booked wrong or
    money moved that the journal does not know about — freeze NEW intents,
    alert ops, and leave every balance exactly as it is for a human to audit.
    Never adjusts a balance itself.

    ``venue_assets_usd`` must be **whole-account** cash, not the futures
    sleeve's equity. Deposits land in the spot wallet, so measuring the
    futures pot alone made the first credited deposit look like a shortfall
    on a perfectly solvent account — see ``DerivGateway.get_cash_assets``.

    ``breakdown`` (where the cash sits, and what the venue will lend against
    it) is recorded but never gates the check — solvency and deployability are
    different questions, and cash in the wrong pot is still the tester's money.
    """
    testers = total_tester_cash()
    headroom = float(venue_assets_usd) - testers
    ok = headroom >= -float(bot_config.POOL_RECON_TOLERANCE_USD)
    snapshot = {
        "at": _now(),
        "venue_assets_usd": round(float(venue_assets_usd), 2),
        "tester_cash_usd": round(testers, 2),
        "house_residual_usd": round(headroom, 2),
        "breakdown": breakdown or {},
        "ok": ok,
    }
    set_meta(_RECON_KEY, json.dumps(snapshot))
    if not ok and not intents_frozen():
        freeze_intents(
            f"reconcile: venue assets ${venue_assets_usd:,.2f} cannot cover "
            f"tester claims ${testers:,.2f}"
        )
    return snapshot


def last_reconcile() -> dict[str, Any] | None:
    raw = get_meta(_RECON_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
