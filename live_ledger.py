"""Live trade ledger — Coinbase perp fills, fully separate from the paper book.

Written by ``execute.py`` (live executor) and read by the dashboard Trading
Log. Never mixes with ``paper_trades``: live headlines must never include
paper house P&L and vice versa.
"""

from __future__ import annotations

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
    notes TEXT
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
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO live_trades (
                cycle_id, source, product_id, instrument, side, qty, entry,
                stop_loss, take_profits_json, order_id, stop_order_id,
                status, opened_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
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
            ),
        )
        return int(cur.lastrowid or 0)


def record_close(
    trade_id: int,
    *,
    exit_price: float,
    pnl_usd: float,
    close_reason: str,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE live_trades
            SET status = 'closed', exit_price = ?, pnl_usd = ?,
                close_reason = ?, closed_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (float(exit_price), float(pnl_usd), close_reason, _now_iso(), trade_id),
        )


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
            "SELECT source, COUNT(*) AS n FROM live_trades WHERE status = 'open' GROUP BY source"
        ).fetchall()
    by_source: dict[str, dict[str, Any]] = {}
    for r in rows:
        closed_n = int(r["closed_n"])
        by_source[str(r["source"])] = {
            "closed": closed_n,
            "pnl_usd": round(float(r["pnl"]), 2),
            "win_rate": (int(r["wins"]) / closed_n) if closed_n else None,
            "open": 0,
        }
    for r in open_rows:
        by_source.setdefault(
            str(r["source"]),
            {"closed": 0, "pnl_usd": 0.0, "win_rate": None, "open": 0},
        )["open"] = int(r["n"])
    total_pnl = round(sum(v["pnl_usd"] for v in by_source.values()), 2)
    return {"by_source": by_source, "total_pnl_usd": total_pnl}


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
) -> bool:
    """Insert at most one NAV row per UTC day. Returns True when written."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO yield_nav_snapshots (
                snapshot_date, nav_usd, collateral_usd, debt_usd, pt_usd,
                health_factor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                today,
                float(nav_usd),
                float(collateral_usd),
                float(debt_usd),
                float(pt_usd),
                health_factor,
                _now_iso(),
            ),
        )
        return cur.rowcount > 0


def get_yield_nav_series(limit: int = 90) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM yield_nav_snapshots ORDER BY snapshot_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]
