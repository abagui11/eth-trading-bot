"""Personal paper books, trade offers, Accept/Reject, and missed-connection joins.

House/agent book stays in ``paper.py``. User capital never mixes into house cash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import bot_config
import config
from models import Suggestion

logger = logging.getLogger(__name__)

LONG_ACTIONS = {"spot_buy", "deriv_buy"}
SHORT_ACTIONS = {"spot_sell", "deriv_sell"}
TRADE_ACTIONS = LONG_ACTIONS | SHORT_ACTIONS

_ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_accounts (
    telegram_id INTEGER PRIMARY KEY,
    starting_usd REAL NOT NULL,
    cash_usd REAL NOT NULL,
    username TEXT,
    opened_at TEXT NOT NULL
);
"""

_USER_POSITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    offer_id TEXT NOT NULL,
    open_cycle_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    product_id TEXT NOT NULL DEFAULT 'ETH-USD',
    qty REAL NOT NULL,
    avg_entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profits TEXT NOT NULL,
    risk_reward REAL,
    suggested_size REAL,
    entry_mode TEXT NOT NULL DEFAULT 'accept',
    status TEXT NOT NULL DEFAULT 'open',
    tps_hit INTEGER NOT NULL DEFAULT 0
);
"""

_USER_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    cycle_id TEXT,
    offer_id TEXT,
    event TEXT NOT NULL,
    side TEXT,
    product_id TEXT NOT NULL DEFAULT 'ETH-USD',
    qty REAL,
    price REAL,
    cash_usd REAL,
    equity_usd REAL,
    position_id INTEGER,
    close_reason TEXT,
    entry_mode TEXT
);
"""

_OFFERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_offers (
    offer_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    suggestion_json TEXT NOT NULL,
    decision_chart_path TEXT,
    structure_chart_path TEXT,
    entry_chart_path TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    house_position_id INTEGER,
    missed_connection_sent INTEGER NOT NULL DEFAULT 0
);
"""

_DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id TEXT NOT NULL,
    telegram_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(offer_id, telegram_id)
);
"""

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_books_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp
        except ValueError:
            return None


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_ACCOUNTS_SCHEMA)
        conn.execute(_USER_POSITIONS_SCHEMA)
        conn.execute(_USER_TRADES_SCHEMA)
        conn.execute(_OFFERS_SCHEMA)
        conn.execute(_DECISIONS_SCHEMA)
        conn.execute(_META_SCHEMA)
        conn.commit()


def get_meta(key: str) -> str | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM user_books_meta WHERE key = ?", (key,)
        ).fetchone()
    return str(row["value"]) if row else None


def set_meta(key: str, value: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_books_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def has_account(telegram_id: int) -> bool:
    return get_account(telegram_id) is not None


def get_account(telegram_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_accounts WHERE telegram_id = ?",
            (int(telegram_id),),
        ).fetchone()
    return dict(row) if row else None


def list_accounts() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM user_accounts ORDER BY opened_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def open_paper_account(
    telegram_id: int,
    amount_usd: float,
    username: str | None = None,
) -> dict:
    """Open a one-time personal demo account at a allowed size."""
    init_db()
    amount = float(amount_usd)
    allowed = {float(x) for x in bot_config.PAPER_ACCOUNT_SIZES}
    if amount not in allowed:
        return {
            "ok": False,
            "reason": "invalid_amount",
            "allowed": sorted(allowed),
        }

    tid = int(telegram_id)
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM user_accounts WHERE telegram_id = ?", (tid,)
        ).fetchone()
        if existing is not None:
            return {
                "ok": False,
                "reason": "already_opened",
                "starting_usd": float(existing["starting_usd"]),
                "cash_usd": float(existing["cash_usd"]),
                "amount_usd": float(existing["starting_usd"]),
            }
        now = _now()
        conn.execute(
            """
            INSERT INTO user_accounts (telegram_id, starting_usd, cash_usd, username, opened_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tid, amount, amount, username, now),
        )
        conn.commit()
    return {
        "ok": True,
        "telegram_id": tid,
        "starting_usd": amount,
        "cash_usd": amount,
        "amount_usd": amount,
        "username": username,
    }


def migrate_funders_to_personal_accounts() -> dict:
    """Create $1k personal accounts for legacy paper_contributions (non-house).

    Does not change house cash. Idempotent for users who already have accounts.
    """
    init_db()
    amount = float(bot_config.PAPER_ACCOUNT_DEFAULT_USD)
    house_id = int(bot_config.HOUSE_CONTRIBUTION_TELEGRAM_ID)
    migrated = 0
    skipped = 0
    with _connect() as conn:
        # paper_contributions may not exist yet on a brand-new DB.
        try:
            contribs = conn.execute(
                """
                SELECT telegram_id, username FROM paper_contributions
                WHERE telegram_id != ?
                ORDER BY created_at ASC
                """,
                (house_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return {"migrated": 0, "skipped": 0, "amount_usd": amount}
        now = _now()
        for row in contribs:
            tid = int(row["telegram_id"])
            existing = conn.execute(
                "SELECT telegram_id FROM user_accounts WHERE telegram_id = ?",
                (tid,),
            ).fetchone()
            if existing is not None:
                skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO user_accounts
                    (telegram_id, starting_usd, cash_usd, username, opened_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tid, amount, amount, row["username"], now),
            )
            migrated += 1
        conn.commit()
    return {"migrated": migrated, "skipped": skipped, "amount_usd": amount}


def _side_from_action(action: str) -> str:
    if action in LONG_ACTIONS:
        return "long"
    if action in SHORT_ACTIONS:
        return "short"
    raise ValueError(f"Not a trade action: {action}")


def _parse_tps(raw: str | list | None) -> list[float]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [float(x) for x in raw]
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [float(x) for x in values]


def _user_equity(cash: float, positions: list[dict], spots: dict[str, float]) -> float:
    total = cash
    for pos in positions:
        side = str(pos["side"])
        qty = float(pos["qty"])
        avg_entry = float(pos["avg_entry"])
        pid = str(pos.get("product_id") or "ETH-USD")
        spot = float(spots.get(pid) or avg_entry)
        if side == "long":
            total += qty * spot
        else:
            total += qty * (2 * avg_entry - spot)
    return total


def _fetch_user_open(conn: sqlite3.Connection, telegram_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM user_positions
        WHERE telegram_id = ? AND status = 'open'
        ORDER BY opened_at ASC, id ASC
        """,
        (int(telegram_id),),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        pos = dict(row)
        pos["take_profits"] = _parse_tps(pos.get("take_profits"))
        out.append(pos)
    return out


def get_user_open_positions(
    telegram_id: int,
    spots: dict[str, float] | None = None,
) -> list[dict]:
    init_db()
    resolved = dict(spots or {})
    with _connect() as conn:
        positions = _fetch_user_open(conn, telegram_id)
    enriched: list[dict] = []
    for pos in positions:
        pid = str(pos.get("product_id") or "ETH-USD")
        spot = float(resolved.get(pid) or pos["avg_entry"])
        qty = float(pos["qty"])
        avg = float(pos["avg_entry"])
        side = str(pos["side"])
        if side == "long":
            unrealized = qty * (spot - avg)
        else:
            unrealized = qty * (avg - spot)
        enriched.append({**pos, "spot": spot, "unrealized_pnl_usd": unrealized})
    return enriched


def get_user_closed_trades(telegram_id: int, limit: int = 25) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM user_trades
            WHERE telegram_id = ? AND event = 'close'
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (int(telegram_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_decisions(telegram_id: int, limit: int = 40) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.*, o.cycle_id, o.product_id, o.suggestion_json,
                   o.decision_chart_path, o.created_at AS offer_created_at
            FROM trade_decisions d
            JOIN trade_offers o ON o.offer_id = d.offer_id
            WHERE d.telegram_id = ?
            ORDER BY COALESCE(d.decided_at, o.created_at) DESC, d.id DESC
            LIMIT ?
            """,
            (int(telegram_id), int(limit)),
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            item["suggestion"] = json.loads(item.pop("suggestion_json"))
        except (json.JSONDecodeError, TypeError, KeyError):
            item["suggestion"] = {}
        out.append(item)
    return out


def get_user_metrics(
    telegram_id: int,
    spots: dict[str, float] | None = None,
) -> dict:
    """Personal book equity / PnL. ok=False if no account."""
    init_db()
    account = get_account(telegram_id)
    if account is None:
        return {"ok": False, "reason": "not_funded"}

    positions = get_user_open_positions(telegram_id, spots=spots)
    resolved = dict(spots or {})
    for pos in positions:
        pid = str(pos.get("product_id") or "ETH-USD")
        resolved.setdefault(pid, float(pos.get("spot") or pos["avg_entry"]))

    starting = float(account["starting_usd"])
    cash = float(account["cash_usd"])
    equity = _user_equity(cash, positions, resolved) if positions else cash
    pnl = equity - starting
    pnl_pct = (pnl / starting * 100) if starting else 0.0
    return {
        "ok": True,
        "telegram_id": int(telegram_id),
        "username": account.get("username"),
        "amount_usd": starting,
        "starting_usd": starting,
        "cash_usd": cash,
        "equity_usd": equity,
        "pnl_usd": pnl,
        "pnl_pct": pnl_pct,
        "open_count": len(positions),
        "share_pct": 100.0,  # personal book = full ownership of own book
        "portfolio_equity_usd": equity,
        "total_contributed_usd": starting,
    }


def compute_user_notional(
    telegram_id: int,
    entry: float,
    spots: dict[str, float] | None = None,
    deploy_pct: float | None = None,
) -> dict:
    """Prospective notional for Accept card copy (notional-only, no qty caps)."""
    account = get_account(telegram_id)
    if account is None:
        return {"ok": False, "reason": "no_account"}
    positions = get_user_open_positions(telegram_id, spots=spots)
    resolved = dict(spots or {})
    cash = float(account["cash_usd"])
    equity = _user_equity(cash, positions, resolved) if positions else cash
    pct = float(deploy_pct if deploy_pct is not None else bot_config.TRADE_DEPLOY_PCT)
    notional = min(max(equity, 0.0) * pct, max(cash, 0.0))
    entry_f = float(entry)
    qty = (notional / entry_f) if entry_f > 0 and notional > 0 else 0.0
    return {
        "ok": True,
        "equity_usd": equity,
        "cash_usd": cash,
        "notional_usd": round(notional, 2),
        "qty": qty,
        "deploy_pct": pct,
    }


def prospective_risk_reward_usd(
    *,
    entry: float,
    stop_loss: float,
    take_profit: float,
    side: str,
    notional_usd: float,
) -> dict:
    """Dollar downside to SL and upside to TP1 for a given notional."""
    if entry <= 0 or notional_usd <= 0:
        return {"risk_usd": 0.0, "reward_usd": 0.0, "qty": 0.0}
    qty = notional_usd / entry
    if side == "long":
        risk = qty * abs(entry - stop_loss)
        reward = qty * abs(take_profit - entry)
    else:
        risk = qty * abs(stop_loss - entry)
        reward = qty * abs(entry - take_profit)
    return {
        "risk_usd": round(risk, 2),
        "reward_usd": round(reward, 2),
        "qty": qty,
    }


def suggestion_to_snapshot(suggestion: Suggestion) -> dict:
    return {
        "action": suggestion.action,
        "size": suggestion.size,
        "entry": suggestion.entry,
        "stop_loss": suggestion.stop_loss,
        "take_profits": list(suggestion.take_profits),
        "risk_reward": suggestion.risk_reward,
        "rationale": suggestion.rationale,
        "order_block": suggestion.order_block,
        "product_id": suggestion.product_id,
        "deploy_pct": suggestion.deploy_pct,
        "entry_tranche": suggestion.entry_tranche,
        "order_block_ref": suggestion.order_block_ref,
    }


def create_trade_offer(
    *,
    cycle_id: str,
    suggestion: Suggestion,
    chart_paths: list[str],
    house_position_id: int | None = None,
    expires_at: str | None = None,
) -> dict | None:
    """Persist a swipeable offer for a trade suggestion. Returns offer row or None."""
    if suggestion.action not in TRADE_ACTIONS:
        return None
    if suggestion.entry is None or suggestion.stop_loss is None:
        return None

    init_db()
    offer_id = str(cycle_id)
    created = _now()
    if expires_at is None:
        exp_dt = datetime.now(timezone.utc) + timedelta(
            minutes=int(bot_config.APPROVAL_WINDOW_MIN)
        )
        expires_at = exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    decision = None
    structure = None
    entry_chart = None
    for path in chart_paths:
        name = str(path).lower()
        if "decision" in name and decision is None:
            decision = path
        elif "structure" in name and structure is None:
            structure = path
        elif "entry" in name and entry_chart is None:
            entry_chart = path
    if decision is None and chart_paths:
        decision = chart_paths[0]
    if structure is None and len(chart_paths) > 1:
        structure = chart_paths[1]
    if entry_chart is None and len(chart_paths) > 2:
        entry_chart = chart_paths[2]

    snap = suggestion_to_snapshot(suggestion)
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO trade_offers (
                offer_id, cycle_id, product_id, suggestion_json,
                decision_chart_path, structure_chart_path, entry_chart_path,
                created_at, expires_at, house_position_id, missed_connection_sent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                offer_id,
                str(cycle_id),
                suggestion.product_id,
                json.dumps(snap),
                decision,
                structure,
                entry_chart,
                created,
                expires_at,
                house_position_id,
            ),
        )
        # Seed pending decisions for every personal account.
        accounts = conn.execute("SELECT telegram_id FROM user_accounts").fetchall()
        for row in accounts:
            conn.execute(
                """
                INSERT OR IGNORE INTO trade_decisions (offer_id, telegram_id, status, decided_at)
                VALUES (?, ?, 'pending', NULL)
                """,
                (offer_id, int(row["telegram_id"])),
            )
        conn.commit()
    return get_offer(offer_id)


def get_offer(offer_id: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM trade_offers WHERE offer_id = ?", (str(offer_id),)
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    try:
        data["suggestion"] = json.loads(data["suggestion_json"])
    except (json.JSONDecodeError, TypeError):
        data["suggestion"] = {}
    return data


def offer_suggestion(offer: dict) -> Suggestion:
    return Suggestion.from_dict(offer.get("suggestion") or {})


def _offer_expired(offer: dict) -> bool:
    exp = _parse_ts(offer.get("expires_at"))
    if exp is None:
        return False
    return datetime.now(timezone.utc) >= exp


def expire_pending_decisions() -> int:
    """Mark pending decisions past offer expiry as expired. Returns count."""
    init_db()
    now = datetime.now(timezone.utc)
    count = 0
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.offer_id, o.expires_at
            FROM trade_decisions d
            JOIN trade_offers o ON o.offer_id = d.offer_id
            WHERE d.status = 'pending'
            """
        ).fetchall()
        stamp = _now()
        for row in rows:
            exp = _parse_ts(row["expires_at"])
            if exp is not None and now >= exp:
                conn.execute(
                    """
                    UPDATE trade_decisions
                    SET status = 'expired', decided_at = ?
                    WHERE id = ?
                    """,
                    (stamp, int(row["id"])),
                )
                count += 1
        conn.commit()
    return count


def get_decision(offer_id: str, telegram_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM trade_decisions
            WHERE offer_id = ? AND telegram_id = ?
            """,
            (str(offer_id), int(telegram_id)),
        ).fetchone()
    return dict(row) if row else None


def _set_decision(
    conn: sqlite3.Connection,
    offer_id: str,
    telegram_id: int,
    status: str,
) -> None:
    conn.execute(
        """
        INSERT INTO trade_decisions (offer_id, telegram_id, status, decided_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(offer_id, telegram_id) DO UPDATE SET
            status = excluded.status,
            decided_at = excluded.decided_at
        """,
        (str(offer_id), int(telegram_id), status, _now()),
    )


def reject_offer(offer_id: str, telegram_id: int) -> dict:
    init_db()
    offer = get_offer(offer_id)
    if offer is None:
        return {"ok": False, "reason": "unknown_offer"}
    account = get_account(telegram_id)
    if account is None:
        return {"ok": False, "reason": "no_account"}

    existing = get_decision(offer_id, telegram_id)
    if existing and existing["status"] not in ("pending",):
        return {
            "ok": False,
            "reason": "already_decided",
            "status": existing["status"],
        }

    with _connect() as conn:
        _set_decision(conn, offer_id, telegram_id, "rejected")
        conn.commit()
    return {"ok": True, "status": "rejected", "offer_id": offer_id}


def _open_user_position(
    conn: sqlite3.Connection,
    *,
    telegram_id: int,
    offer: dict,
    suggestion: Suggestion,
    entry_price: float,
    entry_mode: str,
    spots: dict[str, float],
) -> dict:
    tid = int(telegram_id)
    account = dict(
        conn.execute(
            "SELECT * FROM user_accounts WHERE telegram_id = ?", (tid,)
        ).fetchone()
    )
    positions = _fetch_user_open(conn, tid)
    cash = float(account["cash_usd"])
    equity = _user_equity(cash, positions, spots) if positions else cash
    pct = float(
        suggestion.deploy_pct
        if suggestion.deploy_pct is not None
        else bot_config.TRADE_DEPLOY_PCT
    )
    notional = min(max(equity, 0.0) * pct, max(cash, 0.0))
    if notional < float(bot_config.USER_MIN_DEPLOY_USD):
        return {
            "ok": False,
            "reason": "insufficient_cash",
            "cash_usd": cash,
            "notional_usd": notional,
        }

    entry = float(entry_price)
    qty = notional / entry if entry > 0 else 0.0
    if qty <= 0:
        return {"ok": False, "reason": "invalid_qty"}

    side = _side_from_action(suggestion.action)
    stop = float(suggestion.stop_loss)  # type: ignore[arg-type]
    tps = list(suggestion.take_profits)
    # Recompute display R:R from absolute levels vs this entry.
    risk = abs(entry - stop)
    reward = abs(tps[0] - entry) if tps else 0.0
    rr = (reward / risk) if risk > 0 else suggestion.risk_reward

    cash_after = cash - notional
    now = _now()
    offer_id = str(offer["offer_id"])
    cur = conn.execute(
        """
        INSERT INTO user_positions (
            telegram_id, offer_id, open_cycle_id, opened_at, side, action,
            product_id, qty, avg_entry, stop_loss, take_profits, risk_reward,
            suggested_size, entry_mode, status, tps_hit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0)
        """,
        (
            tid,
            offer_id,
            str(offer["cycle_id"]),
            now,
            side,
            suggestion.action,
            suggestion.product_id,
            qty,
            entry,
            stop,
            json.dumps(tps),
            rr,
            round(notional, 2),
            entry_mode,
        ),
    )
    position_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE user_accounts SET cash_usd = ? WHERE telegram_id = ?",
        (cash_after, tid),
    )
    equity_after = _user_equity(
        cash_after,
        _fetch_user_open(conn, tid),
        spots,
    )
    conn.execute(
        """
        INSERT INTO user_trades (
            telegram_id, ts, cycle_id, offer_id, event, side, product_id,
            qty, price, cash_usd, equity_usd, position_id, entry_mode
        ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tid,
            now,
            str(offer["cycle_id"]),
            offer_id,
            side,
            suggestion.product_id,
            qty,
            entry,
            cash_after,
            equity_after,
            position_id,
            entry_mode,
        ),
    )
    return {
        "ok": True,
        "position_id": position_id,
        "qty": qty,
        "entry": entry,
        "notional_usd": round(notional, 2),
        "cash_usd": cash_after,
        "equity_usd": equity_after,
        "side": side,
        "risk_reward": rr,
        "entry_mode": entry_mode,
    }


def accept_offer(
    offer_id: str,
    telegram_id: int,
    spots: dict[str, float] | None = None,
) -> dict:
    """Accept within the approval window — open at suggestion entry."""
    init_db()
    offer = get_offer(offer_id)
    if offer is None:
        return {"ok": False, "reason": "unknown_offer"}
    if get_account(telegram_id) is None:
        return {"ok": False, "reason": "no_account"}

    existing = get_decision(offer_id, telegram_id)
    if existing and existing["status"] not in ("pending",):
        return {
            "ok": False,
            "reason": "already_decided",
            "status": existing["status"],
        }
    if _offer_expired(offer):
        with _connect() as conn:
            _set_decision(conn, offer_id, telegram_id, "expired")
            conn.commit()
        return {"ok": False, "reason": "expired"}

    suggestion = offer_suggestion(offer)
    entry = float(suggestion.entry)  # type: ignore[arg-type]
    resolved = dict(spots or {})
    resolved.setdefault(suggestion.product_id, entry)

    with _connect() as conn:
        result = _open_user_position(
            conn,
            telegram_id=telegram_id,
            offer=offer,
            suggestion=suggestion,
            entry_price=entry,
            entry_mode="accept",
            spots=resolved,
        )
        if not result.get("ok"):
            conn.rollback()
            return result
        _set_decision(conn, offer_id, telegram_id, "accepted")
        conn.commit()
    result["status"] = "accepted"
    result["offer_id"] = offer_id
    return result


def late_join_offer(
    offer_id: str,
    telegram_id: int,
    mark_price: float,
    spots: dict[str, float] | None = None,
) -> dict:
    """Missed-connection join at current mark; keep absolute SL/TP levels."""
    init_db()
    offer = get_offer(offer_id)
    if offer is None:
        return {"ok": False, "reason": "unknown_offer"}
    if get_account(telegram_id) is None:
        return {"ok": False, "reason": "no_account"}

    existing = get_decision(offer_id, telegram_id)
    if existing and existing["status"] in ("accepted", "late_joined"):
        return {
            "ok": False,
            "reason": "already_decided",
            "status": existing["status"],
        }
    if existing and existing["status"] not in ("rejected", "expired", "pending"):
        return {
            "ok": False,
            "reason": "already_decided",
            "status": existing["status"],
        }

    # Must still have an open house position for this offer (trade still live).
    import paper

    house_open = [
        p
        for p in paper.get_open_positions(spots=spots)
        if str(p.get("open_cycle_id")) == str(offer["cycle_id"])
        or (
            offer.get("house_position_id") is not None
            and int(p.get("id") or 0) == int(offer["house_position_id"])
        )
    ]
    if not house_open:
        return {"ok": False, "reason": "trade_closed"}

    suggestion = offer_suggestion(offer)
    mark = float(mark_price)
    if mark <= 0:
        return {"ok": False, "reason": "invalid_mark"}
    resolved = dict(spots or {})
    resolved[suggestion.product_id] = mark

    with _connect() as conn:
        result = _open_user_position(
            conn,
            telegram_id=telegram_id,
            offer=offer,
            suggestion=suggestion,
            entry_price=mark,
            entry_mode="late_join",
            spots=resolved,
        )
        if not result.get("ok"):
            conn.rollback()
            return result
        _set_decision(conn, offer_id, telegram_id, "late_joined")
        conn.commit()
    result["status"] = "late_joined"
    result["offer_id"] = offer_id
    return result


def decline_missed_connection(offer_id: str, telegram_id: int) -> dict:
    init_db()
    existing = get_decision(offer_id, telegram_id)
    if existing and existing["status"] in ("accepted", "late_joined"):
        return {"ok": False, "reason": "already_in", "status": existing["status"]}
    with _connect() as conn:
        # Keep rejected/expired; just ensure not pending.
        status = (existing or {}).get("status") or "rejected"
        if status == "pending":
            status = "rejected"
        _set_decision(conn, offer_id, telegram_id, status)
        conn.commit()
    return {"ok": True, "status": status, "offer_id": offer_id}


def _close_user_position(
    conn: sqlite3.Connection,
    position: dict,
    exit_price: float,
    reason: str,
    spots: dict[str, float],
) -> None:
    tid = int(position["telegram_id"])
    account = dict(
        conn.execute(
            "SELECT * FROM user_accounts WHERE telegram_id = ?", (tid,)
        ).fetchone()
    )
    cash = float(account["cash_usd"])
    qty = float(position["qty"])
    avg = float(position["avg_entry"])
    side = str(position["side"])
    if side == "long":
        proceeds = qty * exit_price
        pnl = qty * (exit_price - avg)
    else:
        # Short: cash was not reserved beyond notional debit at open;
        # credit entry notional + pnl.
        proceeds = qty * avg + qty * (avg - exit_price)
        pnl = qty * (avg - exit_price)
    cash_after = cash + proceeds
    now = _now()
    conn.execute(
        """
        UPDATE user_positions SET status = 'closed', qty = 0 WHERE id = ?
        """,
        (int(position["id"]),),
    )
    conn.execute(
        "UPDATE user_accounts SET cash_usd = ? WHERE telegram_id = ?",
        (cash_after, tid),
    )
    remaining = _fetch_user_open(conn, tid)
    equity = _user_equity(cash_after, remaining, spots)
    conn.execute(
        """
        INSERT INTO user_trades (
            telegram_id, ts, cycle_id, offer_id, event, side, product_id,
            qty, price, cash_usd, equity_usd, position_id, close_reason, entry_mode
        ) VALUES (?, ?, ?, ?, 'close', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tid,
            now,
            position.get("open_cycle_id"),
            position.get("offer_id"),
            side,
            position.get("product_id"),
            qty,
            exit_price,
            cash_after,
            equity,
            int(position["id"]),
            reason,
            position.get("entry_mode"),
        ),
    )
    logger.info(
        "User %s closed position %s (%s) pnl≈%.2f reason=%s",
        tid,
        position["id"],
        side,
        pnl,
        reason,
    )


def check_user_sl_tp(spots: dict[str, float] | None = None) -> int:
    """Close user positions that hit SL or final TP. Returns closes count."""
    init_db()
    resolved = dict(spots or {})
    closed = 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM user_positions WHERE status = 'open'"
        ).fetchall()
        for row in rows:
            pos = dict(row)
            pos["take_profits"] = _parse_tps(pos.get("take_profits"))
            pid = str(pos.get("product_id") or "ETH-USD")
            spot = float(resolved.get(pid) or 0)
            if spot <= 0:
                continue
            side = str(pos["side"])
            stop = float(pos["stop_loss"])
            tps: list[float] = pos["take_profits"]
            hit_sl = (side == "long" and spot <= stop) or (
                side == "short" and spot >= stop
            )
            if hit_sl:
                _close_user_position(conn, pos, stop, "stop_loss", resolved)
                closed += 1
                continue
            if not tps:
                continue
            # Full exit at first TP for personal books (simple).
            tp1 = float(tps[0])
            hit_tp = (side == "long" and spot >= tp1) or (
                side == "short" and spot <= tp1
            )
            if hit_tp:
                _close_user_position(conn, pos, tp1, "take_profit", resolved)
                closed += 1
        conn.commit()
    return closed


def participation_for_offer(offer_id: str) -> dict:
    """Aggregate Accept/Reject counts and allocated $ for dashboard."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM trade_decisions
            WHERE offer_id = ?
            GROUP BY status
            """,
            (str(offer_id),),
        ).fetchall()
        counts = {str(r["status"]): int(r["n"]) for r in rows}
        allocated = conn.execute(
            """
            SELECT COALESCE(SUM(suggested_size), 0) AS total
            FROM user_positions
            WHERE offer_id = ? AND status = 'open'
            """,
            (str(offer_id),),
        ).fetchone()
        allocated_closed = conn.execute(
            """
            SELECT COALESCE(SUM(suggested_size), 0) AS total
            FROM user_positions
            WHERE offer_id = ?
            """,
            (str(offer_id),),
        ).fetchone()
    return {
        "accepted": counts.get("accepted", 0) + counts.get("late_joined", 0),
        "rejected": counts.get("rejected", 0),
        "expired": counts.get("expired", 0),
        "pending": counts.get("pending", 0),
        "allocated_usd": float(allocated["total"] if allocated else 0),
        "total_sized_usd": float(
            allocated_closed["total"] if allocated_closed else 0
        ),
    }


def participation_by_cycle_id(cycle_id: str) -> dict:
    return participation_for_offer(str(cycle_id))


def house_position_unrealized_r(
    position: dict,
    spot: float,
) -> float | None:
    """Unrealized R multiple vs entry→SL distance."""
    entry = float(position.get("avg_entry") or 0)
    stop = float(position.get("stop_loss") or 0)
    side = str(position.get("side") or "")
    if entry <= 0 or stop <= 0 or spot <= 0:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if side == "long":
        return (spot - entry) / risk
    return (entry - spot) / risk


def find_missed_connection_targets(
    spots: dict[str, float] | None = None,
) -> list[dict]:
    """Offers where house MTM ≥ MISSED_CONNECTION_R and users can still late-join."""
    init_db()
    import paper

    threshold = float(bot_config.MISSED_CONNECTION_R)
    resolved = dict(spots or {})
    house_positions = paper.get_open_positions(spots=resolved)
    by_cycle = {str(p.get("open_cycle_id")): p for p in house_positions}
    targets: list[dict] = []

    with _connect() as conn:
        offers = conn.execute(
            """
            SELECT * FROM trade_offers
            WHERE missed_connection_sent = 0
            """
        ).fetchall()
        for row in offers:
            offer = dict(row)
            offer_id = str(offer["offer_id"])
            cycle_id = str(offer["cycle_id"])
            pos = by_cycle.get(cycle_id)
            if pos is None and offer.get("house_position_id") is not None:
                for p in house_positions:
                    if int(p.get("id") or 0) == int(offer["house_position_id"]):
                        pos = p
                        break
            if pos is None:
                continue
            pid = str(pos.get("product_id") or offer["product_id"])
            spot = float(resolved.get(pid) or pos.get("spot") or 0)
            r_mult = house_position_unrealized_r(pos, spot)
            if r_mult is None or r_mult < threshold:
                continue

            decisions = conn.execute(
                """
                SELECT * FROM trade_decisions
                WHERE offer_id = ? AND status IN ('rejected', 'expired')
                """,
                (offer_id,),
            ).fetchall()
            user_ids = [int(d["telegram_id"]) for d in decisions]
            if not user_ids:
                continue
            targets.append(
                {
                    "offer_id": offer_id,
                    "cycle_id": cycle_id,
                    "product_id": pid,
                    "spot": spot,
                    "r_multiple": r_mult,
                    "telegram_ids": user_ids,
                    "decision_chart_path": offer.get("decision_chart_path"),
                    "house_position": pos,
                }
            )
    return targets


def mark_missed_connection_sent(offer_id: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE trade_offers SET missed_connection_sent = 1
            WHERE offer_id = ?
            """,
            (str(offer_id),),
        )
        conn.commit()


def find_house_position_id_for_cycle(cycle_id: str) -> int | None:
    import paper

    for pos in paper.get_open_positions():
        if str(pos.get("open_cycle_id")) == str(cycle_id):
            return int(pos["id"])
    return None


# --- /me magic-link helpers ---


def create_me_token(telegram_id: int, ttl_sec: int | None = None) -> str:
    """Return ``telegram_id.expiry.sig`` HMAC token."""
    ttl = int(ttl_sec if ttl_sec is not None else config.ME_TOKEN_TTL_SEC)
    expiry = int(time.time()) + ttl
    payload = f"{int(telegram_id)}.{expiry}"
    sig = hmac.new(
        config.ME_TOKEN_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_me_token(token: str) -> int | None:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    tid_s, expiry_s, sig = parts
    try:
        tid = int(tid_s)
        expiry = int(expiry_s)
    except ValueError:
        return None
    if expiry < int(time.time()):
        return None
    payload = f"{tid}.{expiry}"
    expected = hmac.new(
        config.ME_TOKEN_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return None
    return tid


def me_url(telegram_id: int) -> str | None:
    base = config.DASHBOARD_PUBLIC_URL
    if not base:
        return None
    token = create_me_token(telegram_id)
    return f"{base.rstrip('/')}/me?{urlencode({'t': token})}"


def create_session_token(telegram_id: int) -> str:
    return create_me_token(telegram_id, ttl_sec=config.ME_SESSION_TTL_SEC)


LAUNCH_NOTICE = (
    "Personal paper books are live.\n\n"
    "What changed:\n"
    "• The shared Fund ownership model is retired. The public dashboard shows "
    "the agent/house journal; your money is a separate demo book.\n"
    "• If you Funded before, you now have a $1,000 personal demo account.\n"
    "• New users: tap Open account and choose $500 / $1,000 / $2,500 "
    "(demo capital — not real funding).\n"
    "• Every trade suggestion includes Accept / Reject. Only Accept puts your "
    "cash into the trade.\n"
    "• Tap My book for your personal ledger (/me).\n\n"
    "Not financial advice."
)
