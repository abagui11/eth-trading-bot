"""Live trade ledger — Coinbase perp fills, fully separate from the paper book.

Written by ``execute.py`` (live executor) and read by the dashboard Trading
Log. Never mixes with ``paper_trades``: live headlines must never include
paper house P&L and vice versa.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    source TEXT NOT NULL DEFAULT 'hq',            -- 'hq' | 'mill'
    product_id TEXT NOT NULL,
    instrument TEXT NOT NULL,                     -- e.g. ETH_USDC-PERPETUAL
    side TEXT NOT NULL,                           -- 'long' | 'short'
    qty REAL NOT NULL,
    entry REAL NOT NULL,
    stop_loss REAL,
    take_profits_json TEXT,
    order_id TEXT,
    stop_order_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',          -- open | closed | error
    exit_price REAL,
    pnl_usd REAL,
    close_reason TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    notes TEXT,
    fill_type TEXT NOT NULL DEFAULT 'auto',       -- 'auto' | 'manual'
    filled_by INTEGER,                            -- Telegram id on manual fills
    exit_order_ids_json TEXT,                     -- resting bracket/stop order ids
    qty_open REAL,                                -- underlying still on the exchange
    realized_pnl_usd REAL NOT NULL DEFAULT 0,     -- banked from partial exits
    exit_fills_json TEXT                          -- booked exit legs, keyed by order id
);

CREATE TABLE IF NOT EXISTS live_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS yield_nav_snapshots (
    snapshot_date TEXT PRIMARY KEY,               -- UTC YYYY-MM-DD
    nav_usd REAL NOT NULL,
    collateral_usd REAL NOT NULL,
    debt_usd REAL NOT NULL,
    pt_usd REAL NOT NULL,
    health_factor REAL,
    eth_price_usd REAL,
    created_at TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(yield_nav_snapshots)")
        }
        if "eth_price_usd" not in cols:
            conn.execute(
                "ALTER TABLE yield_nav_snapshots ADD COLUMN eth_price_usd REAL"
            )
        live_cols = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)")}
        # Pre-existing rows all predate manual fills, so 'auto' is the correct
        # backfill for the NOT NULL default.
        if "fill_type" not in live_cols:
            conn.execute(
                "ALTER TABLE live_trades ADD COLUMN fill_type TEXT "
                "NOT NULL DEFAULT 'auto'"
            )
        if "filled_by" not in live_cols:
            conn.execute("ALTER TABLE live_trades ADD COLUMN filled_by INTEGER")
        if "exit_order_ids_json" not in live_cols:
            conn.execute(
                "ALTER TABLE live_trades ADD COLUMN exit_order_ids_json TEXT"
            )
        if "realized_pnl_usd" not in live_cols:
            conn.execute(
                "ALTER TABLE live_trades ADD COLUMN realized_pnl_usd REAL "
                "NOT NULL DEFAULT 0"
            )
        if "exit_fills_json" not in live_cols:
            conn.execute("ALTER TABLE live_trades ADD COLUMN exit_fills_json TEXT")
        if "qty_open" not in live_cols:
            conn.execute("ALTER TABLE live_trades ADD COLUMN qty_open REAL")
            # Rows written before partial exits existed were all-or-nothing, so
            # an open row still holds its full size and a closed row holds none.
            conn.execute(
                "UPDATE live_trades SET qty_open = "
                "CASE WHEN status = 'open' THEN qty ELSE 0 END"
            )


# ---------------------------------------------------------------------------
# Live trades
# ---------------------------------------------------------------------------

def record_open(
    *,
    cycle_id: str | None,
    source: str,
    product_id: str,
    instrument: str,
    side: str,
    qty: float,
    entry: float,
    stop_loss: float | None,
    take_profits_json: str | None,
    order_id: str | None,
    stop_order_id: str | None,
    notes: str | None = None,
    fill_type: str = "auto",
    filled_by: int | None = None,
    exit_order_ids: list[str] | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO live_trades (
                cycle_id, source, product_id, instrument, side, qty, entry,
                stop_loss, take_profits_json, order_id, stop_order_id,
                status, opened_at, notes, fill_type, filled_by,
                exit_order_ids_json, qty_open, realized_pnl_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                cycle_id,
                source,
                product_id,
                instrument,
                side,
                float(qty),
                float(entry),
                stop_loss,
                take_profits_json,
                order_id,
                stop_order_id,
                _now_iso(),
                notes,
                fill_type,
                int(filled_by) if filled_by is not None else None,
                json.dumps(list(exit_order_ids or [])),
                float(qty),
            ),
        )
        return int(cur.lastrowid or 0)


def set_exit_orders(trade_id: int, order_ids: list[str]) -> None:
    """Replace the resting exit order ids (re-armed stops, swapped brackets)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE live_trades SET exit_order_ids_json = ? WHERE id = ?",
            (json.dumps(list(order_ids)), trade_id),
        )


def record_partial_exit(
    trade_id: int,
    *,
    exit_qty: float,
    exit_price: float,
    pnl_usd: float,
    order_id: str | None,
    reason: str,
) -> bool:
    """Bank one exit leg. Returns False if this leg was already booked.

    Reconciliation polls the same resting orders repeatedly, so booking is
    keyed by order id to stay idempotent across watchdog passes.
    """
    key = str(order_id or f"{reason}:{_now_iso()}")
    with _connect() as conn:
        row = conn.execute(
            "SELECT qty_open, qty, realized_pnl_usd, exit_fills_json "
            "FROM live_trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        if row is None:
            return False
        booked = json.loads(row["exit_fills_json"] or "{}")
        if key in booked:
            return False
        open_before = row["qty_open"]
        open_before = float(open_before if open_before is not None else row["qty"])
        booked[key] = {
            "qty": float(exit_qty),
            "price": float(exit_price),
            "pnl_usd": float(pnl_usd),
            "reason": reason,
            "at": _now_iso(),
        }
        conn.execute(
            """
            UPDATE live_trades
            SET qty_open = ?, realized_pnl_usd = realized_pnl_usd + ?,
                exit_fills_json = ?
            WHERE id = ?
            """,
            (
                max(0.0, open_before - float(exit_qty)),
                float(pnl_usd),
                json.dumps(booked),
                trade_id,
            ),
        )
    return True


def record_close(
    trade_id: int,
    *,
    exit_price: float,
    pnl_usd: float,
    close_reason: str,
) -> None:
    """Close a trade. ``pnl_usd`` is the final leg; banked partials are added.

    Performance reads sum ``pnl_usd`` over closed rows, so it has to end up
    holding the whole trade's result, not just the last tranche.
    """
    with _connect() as conn:
        conn.execute(
            """
            UPDATE live_trades
            SET status = 'closed', exit_price = ?,
                pnl_usd = COALESCE(realized_pnl_usd, 0) + ?,
                close_reason = ?, closed_at = ?, qty_open = 0
            WHERE id = ? AND status = 'open'
            """,
            (float(exit_price), float(pnl_usd), close_reason, _now_iso(), trade_id),
        )


def get_trade(trade_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM live_trades WHERE id = ?", (trade_id,)
        ).fetchone()
        return dict(row) if row else None


def get_open_trades(source: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM live_trades WHERE status = 'open'"
    args: tuple[Any, ...] = ()
    if source:
        q += " AND source = ?"
        args = (source,)
    q += " ORDER BY opened_at DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def get_closed_trades(
    limit: int = 50, offset: int = 0, source: str | None = None
) -> list[dict[str, Any]]:
    q = "SELECT * FROM live_trades WHERE status = 'closed'"
    args: list[Any] = []
    if source:
        q += " AND source = ?"
        args.append(source)
    q += " ORDER BY closed_at DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q, tuple(args)).fetchall()]


def get_live_performance() -> dict[str, Any]:
    """Realized live P&L per sleeve — never blended with the paper book."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT source,
                   COUNT(*) AS closed_n,
                   COALESCE(SUM(pnl_usd), 0) AS pnl,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins
            FROM live_trades WHERE status = 'closed' GROUP BY source
            """
        ).fetchall()
        open_rows = conn.execute(
            """
            SELECT source, COUNT(*) AS n,
                   COALESCE(SUM(realized_pnl_usd), 0) AS banked
            FROM live_trades WHERE status = 'open' GROUP BY source
            """
        ).fetchall()
        fill_rows = conn.execute(
            """
            SELECT source, fill_type,
                   SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_n,
                   SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_n,
                   COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl_usd END), 0) AS pnl
            FROM live_trades GROUP BY source, fill_type
            """
        ).fetchall()
    by_source: dict[str, dict[str, Any]] = {}
    for r in rows:
        closed_n = int(r["closed_n"])
        by_source[str(r["source"])] = {
            "closed": closed_n,
            "pnl_usd": round(float(r["pnl"]), 2),
            "win_rate": (int(r["wins"]) / closed_n) if closed_n else None,
            "open": 0,
            "banked_open_usd": 0.0,
        }
    # A scale-out on a still-open trade is realized cash. Counting only closed
    # rows hid it entirely: the profit left the position but showed up nowhere.
    # No double count — record_close folds banked partials into pnl_usd.
    for r in open_rows:
        entry = by_source.setdefault(
            str(r["source"]),
            {
                "closed": 0,
                "pnl_usd": 0.0,
                "win_rate": None,
                "open": 0,
                "banked_open_usd": 0.0,
            },
        )
        entry["open"] = int(r["n"])
        banked = round(float(r["banked"] or 0.0), 2)
        entry["banked_open_usd"] = banked
        entry["pnl_usd"] = round(entry["pnl_usd"] + banked, 2)
    # Auto (mill self-fill) vs manual (operator Accept) attribution, so the
    # two entry paths can be judged separately.
    by_fill_type: dict[str, dict[str, Any]] = {}
    for r in fill_rows:
        by_fill_type.setdefault(str(r["source"]), {})[str(r["fill_type"])] = {
            "open": int(r["open_n"] or 0),
            "closed": int(r["closed_n"] or 0),
            "pnl_usd": round(float(r["pnl"] or 0.0), 2),
        }
    total_pnl = round(sum(v["pnl_usd"] for v in by_source.values()), 2)
    return {
        "by_source": by_source,
        "by_fill_type": by_fill_type,
        "total_pnl_usd": total_pnl,
    }


# ---------------------------------------------------------------------------
# Live-run meta (kill switches, daily loss counters)
# ---------------------------------------------------------------------------

def get_meta(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM live_meta WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None


def set_meta(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO live_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# Yield NAV snapshots (cost basis starts at go-live)
# ---------------------------------------------------------------------------

def record_yield_nav(
    *,
    nav_usd: float,
    collateral_usd: float,
    debt_usd: float,
    pt_usd: float,
    health_factor: float | None,
    eth_price_usd: float | None = None,
) -> bool:
    """Upsert today's NAV row (one per UTC day, latest reading wins).

    Keeping the day's row current means pnl_1d compares against yesterday's
    close rather than this morning's first fetch. Returns True when written.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO yield_nav_snapshots (
                snapshot_date, nav_usd, collateral_usd, debt_usd, pt_usd,
                health_factor, eth_price_usd, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                nav_usd = excluded.nav_usd,
                collateral_usd = excluded.collateral_usd,
                debt_usd = excluded.debt_usd,
                pt_usd = excluded.pt_usd,
                health_factor = excluded.health_factor,
                eth_price_usd = COALESCE(excluded.eth_price_usd, yield_nav_snapshots.eth_price_usd),
                created_at = excluded.created_at
            """,
            (
                today,
                float(nav_usd),
                float(collateral_usd),
                float(debt_usd),
                float(pt_usd),
                health_factor,
                float(eth_price_usd) if eth_price_usd else None,
                _now_iso(),
            ),
        )
        return cur.rowcount > 0


def set_yield_eth_price(snapshot_date: str, eth_price_usd: float) -> None:
    """Overwrite ETH/USD on a historical NAV row (Aave-tape repair)."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE yield_nav_snapshots
               SET eth_price_usd = ?
             WHERE snapshot_date = ?
            """,
            (float(eth_price_usd), snapshot_date),
        )


def backfill_yield_eth_price(snapshot_date: str, eth_price_usd: float) -> None:
    """Fill ETH/USD on a historical NAV row when it was recorded without a price."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE yield_nav_snapshots
               SET eth_price_usd = ?
             WHERE snapshot_date = ? AND eth_price_usd IS NULL
            """,
            (float(eth_price_usd), snapshot_date),
        )


def get_yield_nav_series(limit: int = 90) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM yield_nav_snapshots ORDER BY snapshot_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]
