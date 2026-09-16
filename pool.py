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
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import bot_config
import config

logger = logging.getLogger(__name__)

# Long enough that a concurrent writer waits its turn instead of failing.
# Every transaction here spans a handful of statements, so the lock is held
# for microseconds and the queue never builds.
_LOCK_TIMEOUT_SEC = 10.0
# Keyed on the db path, not a bare flag: tests point LEDGER_DB at a fresh file
# per case, and a global "done" would skip creating the schema on the new one.
_schema_ready: set[str] = set()

# One definition of "money this tester could take out", used both for the
# figure quoted to them and for the check inside the transaction. Two spellings
# of this rule would eventually disagree, and the direction it disagrees in is
# paying out money that is committed to an open position.
_AVAILABLE_SQL = "MAX(cash_usd - reserved_usd, 0)"

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

-- Every attempt to prove a wallet against the chain, kept whether it passed
-- or failed. Two reasons it is a table and not a flag: a mismatch is a thing
-- an admin has to look at rather than an error to swallow, and "we checked
-- and it was not them" must be distinguishable from "we could not reach
-- Etherscan" -- otherwise an outage reads as a failed proof and refuses an
-- honest tester's own money.
CREATE TABLE IF NOT EXISTS pool_wallet_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    address TEXT NOT NULL,          -- the registered address under test
    txid TEXT NOT NULL,             -- the deposit offered as proof
    outcome TEXT NOT NULL,          -- see _TERMINAL_CHECKS
    sender TEXT,                    -- who actually sent it, when known
    amount_usd REAL,
    checked_at TEXT NOT NULL,
    alerted_at TEXT
);
-- One row per (proof, claim) pair, updated in place, so a retry after an
-- outage refines the verdict instead of stacking up duplicate history.
CREATE UNIQUE INDEX IF NOT EXISTS pool_wallet_checks_once
    ON pool_wallet_checks (txid, address);

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

-- Every settled transfer Coinbase reports on the deposit address, matched to
-- a tester or not. Keyed on Coinbase's own transaction id, which is what makes
-- auto-crediting safe to retry: the row either exists or it does not, so a
-- restart mid-sweep cannot pay anyone twice.
CREATE TABLE IF NOT EXISTS pool_chain_deposits (
    cb_tx_id TEXT PRIMARY KEY,
    txid TEXT,                              -- network.hash, normalized
    amount_usd REAL NOT NULL,               -- Coinbase's figure, not the claim
    currency TEXT,
    network TEXT,
    status TEXT NOT NULL,                   -- unmatched | credited | baseline
    telegram_id INTEGER,
    deposit_request_id INTEGER,
    first_seen_at TEXT NOT NULL,
    credited_at TEXT,
    alerted_at TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS pool_chain_deposits_txid
    ON pool_chain_deposits (txid);

-- Payouts, with the money debited at request time and the venue leg tracked
-- separately. Coinbase offers no idempotency on sends, so the row has to
-- record that we were ABOUT to send before we send: if the process dies mid
-- call, `submitting` is the only evidence that a payment may exist, and the
-- alternative to that evidence is resending and paying twice.
CREATE TABLE IF NOT EXISTS pool_withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    amount_usd REAL NOT NULL,           -- what the tester receives
    fee_usd REAL NOT NULL DEFAULT 0,    -- network fee, charged on top
    debited_usd REAL NOT NULL,          -- amount + fee, what the ledger took
    to_address TEXT NOT NULL,           -- frozen at request time
    status TEXT NOT NULL,               -- see _WITHDRAWAL_STATES
    cb_tx_id TEXT,
    txid TEXT,
    requested_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by INTEGER,
    submitted_at TEXT,
    settled_at TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS pool_withdrawals_user
    ON pool_withdrawals (telegram_id, requested_at);
CREATE INDEX IF NOT EXISTS pool_withdrawals_status
    ON pool_withdrawals (status);

CREATE TABLE IF NOT EXISTS pool_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_FROZEN_KEY = "intents_frozen"
_RECON_KEY = "last_reconcile"
_CHAIN_BASELINE_KEY = "chain_deposits_baselined"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB, timeout=_LOCK_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def init_db() -> None:
    with _connect():
        pass


@contextmanager
def _write_txn() -> Iterator[sqlite3.Connection]:
    """A serialized read-modify-write over the ledger.

    sqlite begins its implicit transaction at the first *write*, not the first
    read, so the natural shape of a money check —

        SELECT the balance  ->  decide if it covers the amount  ->  write

    — is not atomic by default. Two withdrawals arriving together both read
    the same available cash, both conclude it is enough, and both write; the
    account goes negative and nothing in the journal looks wrong. `BEGIN
    IMMEDIATE` takes the write lock before the read, so the check and the
    write are one indivisible step and the second caller sees the first one's
    effect.

    Anything that decides an amount from a balance belongs in here. A plain
    `_connect()` is fine for reads, and for writes whose correctness does not
    depend on what was just read.
    """
    key = str(config.LEDGER_DB)
    if key not in _schema_ready:
        with _connect():
            pass
        _schema_ready.add(key)

    # isolation_level=None turns off the driver's implicit transactions so
    # BEGIN/COMMIT here mean exactly what they say.
    conn = sqlite3.connect(
        config.LEDGER_DB, timeout=_LOCK_TIMEOUT_SEC, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


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

    **The caller must hold a `_write_txn`.** This reads the balance, adds to
    it in Python, and writes the absolute result, so two of these running
    concurrently on one account both read the old figure and the second
    overwrites the first — a credit that silently never happened. The lost
    update is invisible afterwards: the journal shows both rows, each with a
    plausible `cash_after`, and only the arithmetic between them disagrees.
    `BEGIN IMMEDIATE` is what makes the pair atomic; a plain `_connect()` does
    not, because sqlite starts its implicit transaction at the first write
    rather than the first read.

    Cash kinds (deposit/withdrawal/partial_exit/trade_close/adjustment) move
    ``cash_usd`` by ``amount_usd``. Reserve kinds (reserve/trade_open move it
    up, release moves it down) move ``reserved_usd``. Returns False when the
    (telegram_id, kind, ref) key already exists — the caller treats that as
    "already booked", never as an error. A NULL ``ref`` opts out of that index
    entirely, so anything that could be retried must pass one.
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
    """Book money arriving in a tester's account.

    ``ref`` carries the same meaning as in `debit`: pass the operation's id to
    make a retry safe. The auto-credit path passes the Coinbase transaction
    id; an ad-hoc admin credit has no id and is genuinely repeatable, so it
    gets a unique one — that keeps every balance-moving event covered by the
    dedupe index rather than sitting outside it on a NULL.
    """
    if amount_usd <= 0:
        return {"ok": False, "reason": "bad_amount"}
    if ref is None:
        ref = f"adhoc:{admin_id}:{uuid.uuid4().hex[:12]}"
    with _write_txn() as conn:
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


def withdrawable_usd(telegram_id: int) -> float:
    """The most this tester could take out right now.

    Cash minus everything already committed — intent budgets and the margin
    behind open stakes. Computed here and never accepted from a caller, so a
    "withdraw max" button cannot quote a number the ledger disagrees with, and
    a crafted request cannot name its own ceiling.
    """
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_AVAILABLE_SQL} AS available FROM pool_accounts "
            "WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    return round(float(row["available"]), 2) if row else 0.0


def debit(
    telegram_id: int,
    amount_usd: float,
    *,
    admin_id: int,
    ref: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Book money leaving a tester's account. Cannot touch reserved margin.

    The whole body runs inside `_write_txn`, because the check and the write
    have to be one step: read available cash on one connection and write on
    another, and two withdrawals arriving together both pass a check neither
    of them still satisfies.

    ``ref`` is the identity of the operation, and passing one is what makes a
    retry safe — `_apply_event`'s unique index only covers non-NULL refs, so a
    withdrawal booked with `ref=None` (as this used to) could be replayed and
    charged twice. Callers with an operation id (a withdrawal request, a
    payout) should pass it. An ad-hoc admin correction has no such id and is
    genuinely repeatable — two $500 debits can both be intended — so it gets a
    unique ref rather than a pretence of idempotence.
    """
    if amount_usd <= 0:
        return {"ok": False, "reason": "bad_amount"}
    if ref is None:
        ref = f"adhoc:{admin_id}:{uuid.uuid4().hex[:12]}"

    with _write_txn() as conn:
        account = conn.execute(
            f"SELECT {_AVAILABLE_SQL} AS available FROM pool_accounts "
            "WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if account is None:
            return {"ok": False, "reason": "no_account"}
        available = float(account["available"])
        if amount_usd > available + 1e-9:
            return {"ok": False, "reason": "insufficient_available",
                    "available_usd": round(available, 2)}
        booked = _apply_event(
            conn, telegram_id, kind="withdrawal", amount_usd=-float(amount_usd),
            ref=ref, note=note or f"debited by {admin_id}",
        )
        if not booked:
            return {"ok": False, "reason": "duplicate", "ref": ref}
        after = conn.execute(
            "SELECT cash_usd FROM pool_accounts WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()

    logger.info(
        "pool: debited %s $%.2f by %s (ref %s)", telegram_id, amount_usd,
        admin_id, ref,
    )
    return {"ok": True, "cash_usd": float(after["cash_usd"]), "ref": ref}


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


# A verdict we will not revisit. Everything else -- an Etherscan outage, a
# transfer not indexed yet, one still gathering confirmations -- is temporary
# and gets retried, because refusing a wallet on a transient failure would
# lock a tester out of their funds for an outage that was never their doing.
_TERMINAL_CHECKS = ("verified", "mismatch", "wrong_destination")


def wallet_proofs_to_check(limit: int = 20) -> list[dict[str, Any]]:
    """Unproven wallets paired with a credited deposit that could prove them.

    Only credited deposits count as proof. A pending claim is just a typed
    hash, and a denied one was already judged not to be theirs.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT w.telegram_id, w.address, d.txid "
            "FROM pool_wallets w "
            "JOIN pool_deposit_requests d ON d.telegram_id = w.telegram_id "
            "LEFT JOIN pool_wallet_checks c "
            "       ON c.txid = d.txid AND c.address = w.address "
            "WHERE w.status = 'pending' "
            "  AND d.status = 'credited' AND d.txid IS NOT NULL "
            f"  AND (c.outcome IS NULL OR c.outcome NOT IN {_TERMINAL_CHECKS!r}) "
            "ORDER BY d.id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"telegram_id": int(r["telegram_id"]), "address": str(r["address"]),
             "txid": str(r["txid"])} for r in rows]


def _record_check(
    telegram_id: int, address: str, txid: str, outcome: str,
    *, sender: str | None = None, amount_usd: float | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO pool_wallet_checks (telegram_id, address, txid, "
            "outcome, sender, amount_usd, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(txid, address) DO UPDATE SET "
            "outcome = excluded.outcome, sender = excluded.sender, "
            "amount_usd = excluded.amount_usd, checked_at = excluded.checked_at",
            (telegram_id, address, txid, outcome, sender, amount_usd, _now()),
        )


def mark_wallet_check_alerted(txid: str, address: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE pool_wallet_checks SET alerted_at = ? "
            "WHERE txid = ? AND address = ?",
            (_now(), txid, address),
        )


def wallet_check_mismatches() -> list[dict[str, Any]]:
    """Deposits that did not come from the wallet the tester registered."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_wallet_checks WHERE outcome IN "
            "('mismatch', 'wrong_destination') ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def verify_wallets_onchain(
    lookup: Any, *, deposit_address: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Prove registered wallets against the chain. Returns what to report.

    `lookup(txid, to_address=...)` is injected rather than imported so the
    decision logic can be tested without a network, and so the module that
    holds the money has no dependency on the one that talks to the internet.

    The comparison that matters is one line: the address the tester registered
    against the address that actually sent the funds. Everything around it
    exists to keep a "no" honest — a mismatch is surfaced to an admin instead
    of silently failing, because the usual cause is a tester funding from an
    exchange rather than anything dishonest, and that person still needs
    their money back.
    """
    events: list[dict[str, Any]] = []
    for proof in wallet_proofs_to_check(limit):
        address, txid = proof["address"], proof["txid"]
        telegram_id = proof["telegram_id"]

        result = lookup(txid, to_address=deposit_address)
        if not result.get("ok"):
            reason = str(result.get("reason") or "lookup_failed")
            _record_check(telegram_id, address, txid, reason,
                          sender=result.get("sender"),
                          amount_usd=result.get("amount_usd"))
            if reason in _TERMINAL_CHECKS:
                events.append({"kind": reason, "telegram_id": telegram_id,
                               "address": address, "txid": txid,
                               "actual_to": result.get("actual_to")})
            else:
                logger.info(
                    "pool: wallet %s not proven yet by %s (%s)",
                    address, txid, reason,
                )
            continue

        sender = normalize_address(result.get("sender"))
        amount = float(result.get("amount_usd") or 0.0)

        if sender != address:
            _record_check(telegram_id, address, txid, "mismatch",
                          sender=sender, amount_usd=amount)
            logger.warning(
                "pool: %s registered %s but tx %s came from %s",
                telegram_id, address, txid, sender,
            )
            events.append({"kind": "mismatch", "telegram_id": telegram_id,
                           "address": address, "txid": txid, "sender": sender,
                           "amount_usd": amount})
            continue

        _record_check(telegram_id, address, txid, "verified",
                      sender=sender, amount_usd=amount)
        marked = mark_wallet_verified(address, txid=txid)
        if marked.get("ok") and not marked.get("already"):
            events.append({"kind": "verified", "telegram_id": telegram_id,
                           "address": address, "txid": txid,
                           "amount_usd": amount})
    return events


def payout_target(telegram_id: int) -> dict[str, Any]:
    """Where a withdrawal may go, or why it may not go anywhere yet.

    The single place that decides a destination, so there is one answer to
    "where does this person's money go" rather than a second opinion written
    next to it. `verified` is reached only by `verify_wallets_onchain` finding
    the registered address as the actual sender of a credited deposit.
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


# ---------------------------------------------------------------------------
# Automatic crediting — a tester should not wait on someone being awake
# ---------------------------------------------------------------------------

def observe_chain_deposits(transfers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Credit arrived transfers against filed claims. Returns what happened.

    Attribution is by **transaction hash**, because Coinbase does not report a
    sender for an incoming transfer (see `get_inbound_transfers`). The tester
    supplies the hash on `/deposit`; this matches it and credits **Coinbase's**
    amount, never the claimed one — the venue's number is the one the pool's
    obligation is measured against, and a tester who mistypes their amount
    must not be able to move their own balance.

    A transfer with no matching claim is recorded and flagged, never
    apportioned. Guessing an owner from an amount is how one tester gets
    credited with another's money, and the two transfers already on this
    address are house capital, which is exactly the kind of thing a guess
    would hand to whoever asked most recently.

    Nothing here is destructive or order-dependent: every credit is keyed on
    Coinbase's transaction id, so re-running over the same list is a no-op.
    """
    events: list[dict[str, Any]] = []
    first_run = get_meta(_CHAIN_BASELINE_KEY) is None
    now = _now()

    for transfer in transfers:
        cb_id = str(transfer.get("id") or "")
        if not cb_id:
            continue
        amount = float(transfer.get("amount") or 0)
        if amount <= 0:
            continue
        txid = normalize_txid(transfer.get("txid"))

        # Match on the hash, whatever state the claim is in: an admin may have
        # credited it by hand already, and that must read as settled rather
        # than as an orphan to alert about.
        claim = None
        if txid:
            with _connect() as conn:
                claim = conn.execute(
                    "SELECT * FROM pool_deposit_requests WHERE txid = ? AND "
                    "status != 'denied' ORDER BY id DESC LIMIT 1",
                    (txid,),
                ).fetchone()

        with _connect() as conn:
            known = conn.execute(
                "SELECT * FROM pool_chain_deposits WHERE cb_tx_id = ?", (cb_id,)
            ).fetchone()
            if known is None:
                # Transfers already on the address when the watcher first runs
                # are house capital, not unclaimed tester money, and flagging
                # them would only raise alerts about history nobody can act
                # on. A filed claim overrides that: it is positive evidence
                # the transfer is a tester's, so a deposit that lands during
                # the very first sweep is still credited rather than buried.
                baseline = first_run and claim is None
                conn.execute(
                    "INSERT INTO pool_chain_deposits (cb_tx_id, txid, amount_usd, "
                    "currency, network, status, first_seen_at, note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cb_id, txid, amount, transfer.get("currency"),
                     transfer.get("network"),
                     "baseline" if baseline else "unmatched", now,
                     "pre-existing at watcher start" if baseline else None),
                )
                known = conn.execute(
                    "SELECT * FROM pool_chain_deposits WHERE cb_tx_id = ?",
                    (cb_id,),
                ).fetchone()

        if str(known["status"]) in ("credited", "baseline"):
            continue

        if claim is None:
            events.append({
                "kind": "unmatched", "cb_tx_id": cb_id, "txid": txid,
                "amount_usd": amount,
                "alerted": bool(known["alerted_at"]),
            })
            continue

        telegram_id = int(claim["telegram_id"])
        request_id = int(claim["id"])
        claimed = float(claim["amount_usd"])

        if str(claim["status"]) == "credited":
            # Already paid in by hand. Link the rows so the audit trail shows
            # which on-chain transfer that credit was for, and never re-credit.
            with _connect() as conn:
                conn.execute(
                    "UPDATE pool_chain_deposits SET status = 'credited', "
                    "telegram_id = ?, deposit_request_id = ?, credited_at = ?, "
                    "note = 'credited manually before the watcher saw it' "
                    "WHERE cb_tx_id = ?",
                    (telegram_id, request_id, now, cb_id),
                )
            continue

        result = credit(
            telegram_id,
            amount,
            admin_id=0,  # 0 = the system, not a person
            ref=f"cb_deposit:{cb_id}",
            note=f"auto-credited on arrival, coinbase tx {cb_id}",
        )
        if not result.get("ok"):
            if result.get("reason") != "duplicate":
                logger.error(
                    "pool: auto-credit failed for %s (%s): %s",
                    telegram_id, cb_id, result.get("reason"),
                )
                continue
            # The money is already booked under this ref and only the status
            # write was lost -- a crash between the two. Settle the row so the
            # sweep stops retrying, and stay quiet: the tester was told the
            # first time.
            logger.warning(
                "pool: coinbase tx %s was already booked; settling the row", cb_id
            )
            with _connect() as conn:
                conn.execute(
                    "UPDATE pool_chain_deposits SET status = 'credited', "
                    "telegram_id = ?, deposit_request_id = ?, credited_at = ?, "
                    "note = 'recovered: event existed, status write was lost' "
                    "WHERE cb_tx_id = ?",
                    (telegram_id, request_id, now, cb_id),
                )
                conn.execute(
                    "UPDATE pool_deposit_requests SET status = 'credited', "
                    "decided_at = ?, decided_by = 0 WHERE id = ? AND status = 'pending'",
                    (now, request_id),
                )
            continue

        with _connect() as conn:
            conn.execute(
                "UPDATE pool_chain_deposits SET status = 'credited', "
                "telegram_id = ?, deposit_request_id = ?, credited_at = ? "
                "WHERE cb_tx_id = ?",
                (telegram_id, request_id, now, cb_id),
            )
            conn.execute(
                "UPDATE pool_deposit_requests SET status = 'credited', "
                "decided_at = ?, decided_by = 0 WHERE id = ? AND status = 'pending'",
                (now, request_id),
            )
        logger.info(
            "pool: auto-credited %s $%.2f from coinbase tx %s",
            telegram_id, amount, cb_id,
        )
        events.append({
            "kind": "credited", "cb_tx_id": cb_id, "txid": txid,
            "telegram_id": telegram_id, "request_id": request_id,
            "amount_usd": amount, "claimed_usd": claimed,
            "mismatch": abs(amount - claimed) > 0.01,
            "cash_usd": float(result.get("cash_usd") or 0.0),
        })

    if first_run:
        set_meta(_CHAIN_BASELINE_KEY, now)
    return events


def mark_chain_deposit_alerted(cb_tx_id: str) -> None:
    """Remember an unmatched transfer was reported, so it is raised once."""
    with _connect() as conn:
        conn.execute(
            "UPDATE pool_chain_deposits SET alerted_at = ? WHERE cb_tx_id = ?",
            (_now(), cb_tx_id),
        )


def unmatched_chain_deposits() -> list[dict[str, Any]]:
    """Arrived transfers nobody has claimed — money owed to someone unknown."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_chain_deposits WHERE status = 'unmatched' "
            "ORDER BY first_seen_at"
        ).fetchall()
    return [dict(r) for r in rows]


def assign_chain_deposit(
    cb_tx_id: str, telegram_id: int, *, admin_id: int
) -> dict[str, Any]:
    """Credit an unmatched transfer to a tester, on an admin's say-so.

    The escape hatch for a deposit sent without a hash, or with a mistyped
    one. Credits Coinbase's amount for the same reason the automatic path
    does, and goes through the same idempotency key so a transfer assigned
    twice still only pays once.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pool_chain_deposits WHERE cb_tx_id = ?", (cb_tx_id,)
        ).fetchone()
    if row is None:
        return {"ok": False, "reason": "not_found"}
    if str(row["status"]) == "credited":
        return {"ok": False, "reason": "already_credited",
                "telegram_id": row["telegram_id"]}
    if not is_approved(telegram_id):
        return {"ok": False, "reason": "not_approved"}

    amount = float(row["amount_usd"])
    result = credit(
        telegram_id, amount, admin_id=admin_id,
        ref=f"cb_deposit:{cb_tx_id}",
        note=f"assigned by {admin_id}, coinbase tx {cb_tx_id}",
    )
    if not result.get("ok"):
        return result
    with _connect() as conn:
        conn.execute(
            "UPDATE pool_chain_deposits SET status = 'credited', telegram_id = ?, "
            "credited_at = ?, note = ? WHERE cb_tx_id = ?",
            (telegram_id, _now(), f"assigned by admin {admin_id}", cb_tx_id),
        )
    logger.info(
        "pool: %s assigned coinbase tx %s ($%.2f) to %s",
        admin_id, cb_tx_id, amount, telegram_id,
    )
    return {"ok": True, "telegram_id": telegram_id, "amount_usd": amount,
            "cash_usd": result.get("cash_usd")}


# ---------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------

# requested -> approved -> submitting -> submitted -> settled
#                    \-> rejected          \-> unknown (needs a human)
#                                          \-> failed (refunded)
_PAYOUT_HALT_KEY = "payouts_halted"


def max_withdrawal_usd(telegram_id: int) -> float:
    """The largest amount this tester could ask for and have it clear.

    Available cash less a fee reserve, because the network fee is charged on
    top of the send: quoting the raw balance would produce a "withdraw max"
    that the balance cannot actually cover, failing at the exact moment
    someone is trying to take their money out.
    """
    available = withdrawable_usd(telegram_id)
    reserve = float(bot_config.POOL_WITHDRAWAL_FEE_RESERVE_USD)
    return round(max(min(available - reserve,
                         float(bot_config.POOL_MAX_WITHDRAWAL_USD)), 0.0), 2)


def withdrawn_since(telegram_id: int | None, hours: float = 24.0) -> float:
    """Sum of payouts not in a refunded state over a window, for the caps."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    sql = (
        "SELECT COALESCE(SUM(debited_usd), 0) AS total FROM pool_withdrawals "
        "WHERE requested_at >= ? AND status NOT IN ('rejected', 'failed')"
    )
    params: list[Any] = [cutoff]
    if telegram_id is not None:
        sql += " AND telegram_id = ?"
        params.append(telegram_id)
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return round(float(row["total"]), 2)


def payouts_halted() -> str | None:
    return get_meta(_PAYOUT_HALT_KEY)


def halt_payouts(reason: str) -> None:
    """Stop the payout queue. Used when an outcome is unknown.

    With no way to ask Coinbase whether a send already happened, continuing
    past an ambiguous failure is guessing with someone else's money.
    """
    set_meta(_PAYOUT_HALT_KEY, reason)
    logger.error("pool: PAYOUTS HALTED — %s", reason)


def resume_payouts() -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM pool_meta WHERE key = ?", (_PAYOUT_HALT_KEY,))
    logger.warning("pool: payouts resumed by operator")


def request_withdrawal(telegram_id: int, amount_usd: float) -> dict[str, Any]:
    """Debit the tester and queue a payout. The debit happens HERE.

    Taking the money at request time, inside the same transaction that checks
    the balance, is what stops two requests spending one balance. The debit is
    ``amount + fee reserve``; the reserve is trued up to the real fee once
    Coinbase reports it, and refunded in full if the payout never leaves.
    """
    amount_usd = round(float(amount_usd), 2)
    minimum = float(bot_config.POOL_MIN_WITHDRAWAL_USD)

    halted = payouts_halted()
    if halted:
        return {"ok": False, "reason": "halted", "detail": halted}
    if not bot_config.POOL_PAYOUTS_ENABLED:
        return {"ok": False, "reason": "disabled"}
    if not is_approved(telegram_id):
        return {"ok": False, "reason": "not_approved"}
    if amount_usd < minimum:
        return {"ok": False, "reason": "below_minimum", "minimum_usd": minimum}
    if amount_usd > float(bot_config.POOL_MAX_WITHDRAWAL_USD):
        return {"ok": False, "reason": "above_max",
                "maximum_usd": float(bot_config.POOL_MAX_WITHDRAWAL_USD)}

    target = payout_target(telegram_id)
    if not target.get("ok"):
        return {"ok": False, "reason": target.get("reason", "no_payout_address"),
                "detail": target}

    reserve = float(bot_config.POOL_WITHDRAWAL_FEE_RESERVE_USD)
    debit_total = round(amount_usd + reserve, 2)

    # Caps bound the blast radius of a bug or a stolen account, so they are
    # checked against what has already gone out today, not just this request.
    user_today = withdrawn_since(telegram_id)
    if user_today + debit_total > float(bot_config.POOL_MAX_USER_DAILY_WITHDRAWAL_USD):
        return {"ok": False, "reason": "user_daily_cap",
                "already_usd": user_today,
                "cap_usd": float(bot_config.POOL_MAX_USER_DAILY_WITHDRAWAL_USD)}
    global_today = withdrawn_since(None)
    if global_today + debit_total > float(
        bot_config.POOL_MAX_GLOBAL_DAILY_WITHDRAWAL_USD
    ):
        return {"ok": False, "reason": "global_daily_cap",
                "already_usd": global_today,
                "cap_usd": float(bot_config.POOL_MAX_GLOBAL_DAILY_WITHDRAWAL_USD)}

    address = str(target["address"])
    with _write_txn() as conn:
        row = conn.execute(
            f"SELECT {_AVAILABLE_SQL} AS available FROM pool_accounts "
            "WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "no_account"}
        available = float(row["available"])
        if debit_total > available + 1e-9:
            return {"ok": False, "reason": "insufficient_available",
                    "available_usd": round(available, 2),
                    "max_usd": max(round(available - reserve, 2), 0.0),
                    "fee_reserve_usd": reserve}

        cur = conn.execute(
            "INSERT INTO pool_withdrawals (telegram_id, amount_usd, fee_usd, "
            "debited_usd, to_address, status, requested_at) "
            "VALUES (?, ?, ?, ?, ?, 'requested', ?)",
            (telegram_id, amount_usd, reserve, debit_total, address, _now()),
        )
        wid = int(cur.lastrowid)
        booked = _apply_event(
            conn, telegram_id, kind="withdrawal", amount_usd=-debit_total,
            ref=f"withdrawal:{wid}",
            note=f"withdrawal #{wid} to {address[:10]}…",
        )
        if not booked:
            raise RuntimeError(f"withdrawal {wid} collided on its own ref")

    logger.info(
        "pool: withdrawal #%s user %s $%.2f (+$%.2f reserve) to %s",
        wid, telegram_id, amount_usd, reserve, address,
    )
    return {"ok": True, "withdrawal_id": wid, "amount_usd": amount_usd,
            "fee_reserve_usd": reserve, "debited_usd": debit_total,
            "address": address,
            "cash_usd": float((get_account(telegram_id) or {}).get("cash_usd") or 0)}


def get_withdrawal(withdrawal_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pool_withdrawals WHERE id = ?", (withdrawal_id,)
        ).fetchone()
    return dict(row) if row else None


def pending_withdrawals(status: str = "approved") -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_withdrawals WHERE status = ? ORDER BY id",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


def decide_withdrawal(
    withdrawal_id: int, *, admin_id: int, approve: bool, note: str | None = None
) -> dict[str, Any]:
    """Approve a queued payout for sending, or reject and refund it."""
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT * FROM pool_withdrawals WHERE id = ?", (withdrawal_id,)
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if str(row["status"]) != "requested":
            return {"ok": False, "reason": "already_decided",
                    "status": row["status"]}

        if approve:
            conn.execute(
                "UPDATE pool_withdrawals SET status = 'approved', approved_at = ?, "
                "approved_by = ?, note = ? WHERE id = ?",
                (_now(), admin_id, note, withdrawal_id),
            )
        else:
            conn.execute(
                "UPDATE pool_withdrawals SET status = 'rejected', approved_at = ?, "
                "approved_by = ?, note = ? WHERE id = ?",
                (_now(), admin_id, note, withdrawal_id),
            )
            _apply_event(
                conn, int(row["telegram_id"]), kind="adjustment",
                amount_usd=float(row["debited_usd"]),
                ref=f"withdrawal:{withdrawal_id}:refund",
                note=f"withdrawal #{withdrawal_id} rejected",
            )

    logger.info(
        "pool: withdrawal #%s %s by %s", withdrawal_id,
        "approved" if approve else "rejected", admin_id,
    )
    return {"ok": True, "withdrawal_id": withdrawal_id,
            "telegram_id": int(row["telegram_id"]),
            "amount_usd": float(row["amount_usd"]),
            "approved": approve}


def mark_withdrawal_submitting(withdrawal_id: int) -> bool:
    """Record that a send is ABOUT to happen. Must precede the API call.

    Without this row there is no evidence a payment might exist if the process
    dies mid-call, and the only alternative to evidence is resending — which,
    with no venue-side idempotency, pays twice.
    """
    with _write_txn() as conn:
        cur = conn.execute(
            "UPDATE pool_withdrawals SET status = 'submitting', submitted_at = ? "
            "WHERE id = ? AND status = 'approved'",
            (_now(), withdrawal_id),
        )
        return cur.rowcount > 0


def mark_withdrawal_submitted(
    withdrawal_id: int, *, cb_tx_id: str, fee_usd: float, txid: str | None = None
) -> dict[str, Any]:
    """The venue accepted it. True up the fee reserve against the real fee."""
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT * FROM pool_withdrawals WHERE id = ?", (withdrawal_id,)
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}

        amount = float(row["amount_usd"])
        reserved = float(row["debited_usd"])
        actual = round(amount + float(fee_usd), 2)
        refund = round(reserved - actual, 2)

        conn.execute(
            "UPDATE pool_withdrawals SET status = 'submitted', cb_tx_id = ?, "
            "txid = ?, fee_usd = ?, debited_usd = ? WHERE id = ?",
            (cb_tx_id, txid, float(fee_usd), actual, withdrawal_id),
        )
        if abs(refund) >= 0.01:
            # The reserve is headroom for a gas spike, not a charge. Whatever
            # was not needed goes straight back.
            _apply_event(
                conn, int(row["telegram_id"]), kind="adjustment",
                amount_usd=refund,
                ref=f"withdrawal:{withdrawal_id}:fee_trueup",
                note=f"fee reserve trued up to ${fee_usd:.2f}",
            )
    logger.info(
        "pool: withdrawal #%s submitted (cb %s, fee $%.4f, refund $%.2f)",
        withdrawal_id, cb_tx_id, fee_usd, refund,
    )
    return {"ok": True, "refunded_usd": refund, "fee_usd": float(fee_usd)}


def mark_withdrawal_settled(withdrawal_id: int, *, txid: str | None = None) -> None:
    with _write_txn() as conn:
        conn.execute(
            "UPDATE pool_withdrawals SET status = 'settled', settled_at = ?, "
            "txid = COALESCE(?, txid) WHERE id = ?",
            (_now(), txid, withdrawal_id),
        )


def mark_withdrawal_failed(withdrawal_id: int, *, reason: str) -> dict[str, Any]:
    """The venue refused it and no money moved. Give the tester theirs back."""
    with _write_txn() as conn:
        row = conn.execute(
            "SELECT * FROM pool_withdrawals WHERE id = ?", (withdrawal_id,)
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if str(row["status"]) in ("failed", "rejected"):
            return {"ok": False, "reason": "already_refunded"}

        conn.execute(
            "UPDATE pool_withdrawals SET status = 'failed', note = ? WHERE id = ?",
            (reason[:300], withdrawal_id),
        )
        _apply_event(
            conn, int(row["telegram_id"]), kind="adjustment",
            amount_usd=float(row["debited_usd"]),
            ref=f"withdrawal:{withdrawal_id}:refund",
            note=f"payout failed: {reason[:120]}",
        )
    logger.warning("pool: withdrawal #%s failed and refunded — %s",
                   withdrawal_id, reason)
    return {"ok": True, "telegram_id": int(row["telegram_id"]),
            "refunded_usd": float(row["debited_usd"])}


def mark_withdrawal_unknown(withdrawal_id: int, *, reason: str) -> None:
    """The send may or may not have happened. Do NOT refund, do NOT retry.

    Refunding could hand back money that already left, and retrying could send
    it twice — Coinbase will not tell us which. The money stays debited, the
    queue halts, and a human reconciles against the balance.
    """
    with _write_txn() as conn:
        conn.execute(
            "UPDATE pool_withdrawals SET status = 'unknown', note = ? WHERE id = ?",
            (reason[:300], withdrawal_id),
        )
    halt_payouts(f"withdrawal #{withdrawal_id} outcome unknown: {reason[:120]}")


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
    already reserved), and the read and the reserve happen in one transaction
    so concurrent Accepts cannot each size against the same dollars. They used
    to: the balance was read on one connection and reserved on another, so two
    cards accepted together both computed their budget from the pre-reserve
    figure. The overlap was small because the budget is a fraction of a
    fraction, which is exactly why it would never have shown up in a balance
    anyone eyeballed.
    """
    frozen = intents_frozen()
    if frozen:
        return {"ok": False, "reason": "frozen", "detail": frozen}
    if not is_approved(telegram_id):
        return {"ok": False, "reason": "not_approved"}

    with _write_txn() as conn:
        account = conn.execute(
            f"SELECT cash_usd, {_AVAILABLE_SQL} AS available FROM pool_accounts "
            "WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if account is None or float(account["cash_usd"]) <= 0:
            return {"ok": False, "reason": "not_funded"}
        cash = float(account["cash_usd"])
        if cash < float(bot_config.POOL_MIN_EQUITY_USD):
            return {"ok": False, "reason": "below_min_equity",
                    "minimum_usd": float(bot_config.POOL_MIN_EQUITY_USD)}
        risk = round(float(account["available"]) * float(bot_config.POOL_RISK_PCT), 2)
        if risk <= 0:
            return {"ok": False, "reason": "no_available_cash"}

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
    with _write_txn() as conn:
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
    live_pending cycle ids + fillable mill ideas). Anything else is a promise
    that can no longer be kept, so the reserve goes back and the tester is told.

    `POOL_INTENT_TTL_MIN` is a backstop on top of that, and it is here because
    the ref-based release depends on another subsystem's bookkeeping being
    right. It was not: a tester's Accept exempted the mill idea from expiry, so
    the ref stayed "active" indefinitely and the reserve was held with the
    tester never hearing back. That root cause is fixed, but an Accept going
    quiet with someone's money held is bad enough that it should not be
    reachable by any single bug. The TTL sits above the longest window in which
    a ref could still legitimately fire, so it never races a real fill.
    """
    released: list[dict[str, Any]] = []
    ttl_min = int(getattr(bot_config, "POOL_INTENT_TTL_MIN", 0) or 0)
    cutoff = (
        (datetime.now(timezone.utc)
         - timedelta(minutes=ttl_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if ttl_min > 0 else None
    )
    with _write_txn() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_intents WHERE status = 'pending'"
        ).fetchall()
        for row in rows:
            intent = dict(row)
            expired = cutoff is not None and str(intent["created_at"]) < cutoff
            if str(intent["ref"]) in active_refs and not expired:
                continue
            if expired and str(intent["ref"]) in active_refs:
                logger.warning(
                    "Intent %s on %s hit the TTL while its ref still looked "
                    "active — releasing anyway", intent["id"], intent["ref"],
                )
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
    with _write_txn() as conn:
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
    with _write_txn() as conn:
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
    with _write_txn() as conn:
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
