"""Paper portfolio tracker — USD notional sizing with per-product qty bounds."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

import bot_config
import config
from models import Suggestion

LONG_ACTIONS = {"spot_buy", "deriv_buy"}
SHORT_ACTIONS = {"spot_sell", "deriv_sell"}
TRADE_ACTIONS = LONG_ACTIONS | SHORT_ACTIONS

logger = logging.getLogger(__name__)

# Close events queued during a paper mutation; flushed after commit.
_PENDING_OUTCOMES: list[dict] = []


def _pos_qty(pos) -> float:
    """Prefer qty; fall back to eth_qty for legacy rows."""
    if pos.get("qty") is not None:
        return float(pos["qty"])
    return float(pos.get("eth_qty") or 0)


def _pos_product(pos) -> str:
    """Prefer product_id; default ETH-USD."""
    return str(pos.get("product_id") or "ETH-USD")


def _resolve_spots(
    spot_price: float | None = None,
    spots: dict[str, float] | None = None,
) -> dict[str, float]:
    """Merge spot dict; apply spot_price to ETH-USD; else fetch from research."""
    out: dict[str, float] = {}
    if spots:
        for key, value in spots.items():
            try:
                fv = float(value)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                out[str(key)] = fv
    if spot_price is not None:
        try:
            fv = float(spot_price)
            if fv > 0:
                out["ETH-USD"] = fv
        except (TypeError, ValueError):
            pass
    missing = [pid for pid in bot_config.TRADED_PRODUCTS if pid not in out]
    if not out or missing:
        try:
            import research

            fetched = research.get_spot_prices(
                missing if out and missing else None
            )
            for key, value in fetched.items():
                try:
                    fv = float(value)
                except (TypeError, ValueError):
                    continue
                if fv > 0:
                    out.setdefault(str(key), fv)
        except Exception:
            pass
    return out


def _spot_for(product_id: str | None, spots: dict[str, float]) -> float:
    pid = str(product_id or "ETH-USD")
    value = spots.get(pid)
    if value is not None and float(value) > 0:
        return float(value)
    return 0.0


_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    starting_usd REAL NOT NULL,
    cash_usd REAL NOT NULL,
    last_cycle_id TEXT,
    last_spot REAL
);
"""

_POSITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    open_cycle_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    eth_qty REAL NOT NULL,
    avg_entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profits TEXT NOT NULL,
    risk_reward REAL,
    suggested_size REAL,
    status TEXT NOT NULL DEFAULT 'open'
);
"""

_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cycle_id TEXT,
    event TEXT NOT NULL,
    side TEXT,
    eth_qty REAL,
    price REAL,
    cash_usd REAL,
    equity_usd REAL,
    position_id INTEGER,
    close_reason TEXT
);
"""

_TRADES_ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER,
    ts TEXT NOT NULL,
    cycle_id TEXT,
    event TEXT NOT NULL,
    side TEXT,
    eth_qty REAL,
    price REAL,
    cash_usd REAL,
    equity_usd REAL,
    position_id INTEGER,
    close_reason TEXT,
    archived_at TEXT NOT NULL,
    epoch_label TEXT NOT NULL
);
"""

_POSITIONS_ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_positions_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER,
    open_cycle_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    eth_qty REAL NOT NULL,
    avg_entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profits TEXT NOT NULL,
    risk_reward REAL,
    suggested_size REAL,
    status TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    epoch_label TEXT NOT NULL
);
"""

_EPOCHS_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_epochs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    starting_usd REAL NOT NULL,
    ended_at TEXT NOT NULL,
    archived_trade_rows INTEGER NOT NULL DEFAULT 0,
    archived_position_rows INTEGER NOT NULL DEFAULT 0
);
"""

_CONTRIBUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_contributions (
    telegram_id INTEGER PRIMARY KEY,
    amount_usd REAL NOT NULL,
    created_at TEXT NOT NULL,
    username TEXT
);
"""

# Legacy single-position columns on paper_state (migrated to paper_positions).
_LEGACY_POSITION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("side", "TEXT"),
    ("eth_qty", "REAL"),
    ("avg_entry", "REAL"),
    ("action", "TEXT"),
    ("stop_loss", "REAL"),
    ("take_profits", "TEXT"),
    ("risk_reward", "REAL"),
    ("suggested_size", "REAL"),
    ("opened_at", "TEXT"),
    ("open_cycle_id", "TEXT"),
    ("epoch_started_at", "TEXT"),
    ("epoch_label", "TEXT"),
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_legacy_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_state)").fetchall()}
    for name, col_type in _LEGACY_POSITION_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE paper_state ADD COLUMN {name} {col_type}")


def _ensure_trade_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
    if "position_id" not in cols:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN position_id INTEGER")
    if "close_reason" not in cols:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN close_reason TEXT")
    if "product_id" not in cols:
        conn.execute(
            "ALTER TABLE paper_trades ADD COLUMN product_id TEXT NOT NULL DEFAULT 'ETH-USD'"
        )
    if "qty" not in cols:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN qty REAL")


def _ensure_trade_archive_columns(conn: sqlite3.Connection) -> None:
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(paper_trades_archive)").fetchall()
    }
    if "product_id" not in cols:
        conn.execute(
            "ALTER TABLE paper_trades_archive ADD COLUMN product_id TEXT NOT NULL DEFAULT 'ETH-USD'"
        )
    if "qty" not in cols:
        conn.execute("ALTER TABLE paper_trades_archive ADD COLUMN qty REAL")


def _ensure_position_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_positions)").fetchall()}
    if "order_block_ref" not in cols:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN order_block_ref TEXT")
    if "entry_tranches" not in cols:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN entry_tranches TEXT")
    if "product_id" not in cols:
        conn.execute(
            "ALTER TABLE paper_positions ADD COLUMN product_id TEXT NOT NULL DEFAULT 'ETH-USD'"
        )
    if "qty" not in cols:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN qty REAL")
    if "tps_hit" not in cols:
        conn.execute(
            "ALTER TABLE paper_positions ADD COLUMN tps_hit INTEGER NOT NULL DEFAULT 0"
        )
    if "mfe_pct" not in cols:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN mfe_pct REAL")
    if "mae_pct" not in cols:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN mae_pct REAL")
    # How far the M5 barrier walk has already got. NULL on every pre-existing
    # row, which starts the walk at the next cycle rather than reaching back
    # over closed history — the fix is forward-only by construction.
    if "path_checked_at" not in cols:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN path_checked_at TEXT")


def _ensure_position_archive_columns(conn: sqlite3.Connection) -> None:
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(paper_positions_archive)").fetchall()
    }
    if "product_id" not in cols:
        conn.execute(
            "ALTER TABLE paper_positions_archive ADD COLUMN product_id TEXT NOT NULL DEFAULT 'ETH-USD'"
        )
    if "qty" not in cols:
        conn.execute("ALTER TABLE paper_positions_archive ADD COLUMN qty REAL")


def _ensure_state_contribution_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_state)").fetchall()}
    if "total_contributed_usd" not in cols:
        conn.execute("ALTER TABLE paper_state ADD COLUMN total_contributed_usd REAL")


def _ensure_house_contribution(conn: sqlite3.Connection, starting_usd: float) -> None:
    """Seed house stake row and backfill total_contributed_usd when missing."""
    row = conn.execute(
        "SELECT total_contributed_usd, starting_usd, cash_usd FROM paper_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return

    total = row["total_contributed_usd"]
    if total is None:
        seed = float(row["starting_usd"] if row["starting_usd"] is not None else starting_usd)
        conn.execute(
            "UPDATE paper_state SET total_contributed_usd = ? WHERE id = 1",
            (seed,),
        )
        total = seed

    house_id = int(bot_config.HOUSE_CONTRIBUTION_TELEGRAM_ID)
    existing = conn.execute(
        "SELECT telegram_id FROM paper_contributions WHERE telegram_id = ?",
        (house_id,),
    ).fetchone()
    if existing is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """
            INSERT INTO paper_contributions (telegram_id, amount_usd, created_at, username)
            VALUES (?, ?, ?, ?)
            """,
            (house_id, float(total), now, "house"),
        )


def _parse_entry_tranches(raw: str | list | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(x) for x in values]


def _merge_entry_tranches(existing: list[str], new_tranche: str | None) -> list[str]:
    if not new_tranche:
        return existing
    merged = list(existing)
    if new_tranche not in merged:
        merged.append(new_tranche)
    return merged


def _ensure_state_epoch_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_state)").fetchall()}
    if "epoch_started_at" not in cols:
        conn.execute("ALTER TABLE paper_state ADD COLUMN epoch_started_at TEXT")
    if "epoch_label" not in cols:
        conn.execute("ALTER TABLE paper_state ADD COLUMN epoch_label TEXT")


def _migrate_legacy_position(conn: sqlite3.Connection) -> None:
    """Move a single open row from paper_state into paper_positions (one-time)."""
    count = conn.execute(
        "SELECT COUNT(*) FROM paper_positions WHERE status = 'open'"
    ).fetchone()[0]
    if count > 0:
        return

    row = conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone()
    if row is None:
        return
    data = dict(row)
    side = str(data.get("side") or "flat")
    eth_qty = float(data.get("eth_qty") or 0)
    if side == "flat" or eth_qty <= 0:
        return
    if data.get("open_cycle_id") is None or data.get("stop_loss") is None:
        return

    conn.execute(
        """
        INSERT INTO paper_positions (
            open_cycle_id, opened_at, side, action, eth_qty, qty, product_id, avg_entry,
            stop_loss, take_profits, risk_reward, suggested_size, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            data["open_cycle_id"],
            data.get("opened_at")
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            side,
            data.get("action") or side,
            eth_qty,
            eth_qty,
            "ETH-USD",
            float(data["avg_entry"]),
            float(data["stop_loss"]),
            data.get("take_profits") or "[]",
            data.get("risk_reward"),
            data.get("suggested_size"),
        ),
    )
    conn.execute(
        """
        UPDATE paper_state
        SET side = 'flat', eth_qty = 0, avg_entry = NULL,
            action = NULL, stop_loss = NULL, take_profits = NULL,
            risk_reward = NULL, suggested_size = NULL,
            opened_at = NULL, open_cycle_id = NULL
        WHERE id = 1
        """
    )


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_STATE_SCHEMA)
        conn.execute(_POSITIONS_SCHEMA)
        conn.execute(_TRADES_SCHEMA)
        conn.execute(_TRADES_ARCHIVE_SCHEMA)
        conn.execute(_POSITIONS_ARCHIVE_SCHEMA)
        conn.execute(_EPOCHS_SCHEMA)
        conn.execute(_CONTRIBUTIONS_SCHEMA)
        _ensure_legacy_columns(conn)
        _ensure_trade_columns(conn)
        _ensure_trade_archive_columns(conn)
        _ensure_state_epoch_columns(conn)
        _ensure_state_contribution_columns(conn)
        _ensure_position_columns(conn)
        _ensure_position_archive_columns(conn)
        row = conn.execute("SELECT id FROM paper_state WHERE id = 1").fetchone()
        if row is None:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            starting = float(config.PAPER_PORTFOLIO_VALUE)
            conn.execute(
                """
                INSERT INTO paper_state (
                    id, starting_usd, cash_usd, epoch_started_at, epoch_label,
                    total_contributed_usd
                )
                VALUES (1, ?, ?, ?, ?, ?)
                """,
                (
                    starting,
                    starting,
                    now,
                    bot_config.PAPER_EPOCH_LABEL,
                    starting,
                ),
            )
        _ensure_house_contribution(conn, float(config.PAPER_PORTFOLIO_VALUE))
        _migrate_legacy_position(conn)
        conn.commit()
    # Personal books / offers live alongside the house book.
    import user_books

    user_books.init_db()
    user_books.migrate_funders_to_personal_accounts()


def get_sizing_basis(
    spot_price: float | None = None,
    spots: dict[str, float] | None = None,
) -> tuple[float, float]:
    """Return ``(equity_usd, cash_usd)`` for fixed-fraction trade sizing."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT cash_usd FROM paper_state WHERE id = 1").fetchone()
        cash = float(row["cash_usd"]) if row else config.PAPER_PORTFOLIO_VALUE
        positions = _fetch_open_positions(conn)

    resolved = _resolve_spots(spot_price, spots)
    if positions and not resolved:
        # Fall back to first position entry if no spots available.
        resolved = {_pos_product(positions[0]): float(positions[0]["avg_entry"])}
    elif positions:
        for pos in positions:
            pid = _pos_product(pos)
            if _spot_for(pid, resolved) <= 0:
                resolved.setdefault(pid, float(pos["avg_entry"]))

    if not resolved:
        equity = cash
    else:
        equity = _equity(cash, positions, resolved)

    return max(equity, 0.0), max(cash, 0.0)


def _equity(
    cash: float,
    positions: list[dict],
    spots: dict[str, float],
) -> float:
    total = cash
    for pos in positions:
        side = str(pos["side"])
        qty = _pos_qty(pos)
        avg_entry = float(pos["avg_entry"])
        spot = _spot_for(_pos_product(pos), spots)
        if side == "long":
            total += qty * spot
        elif side == "short":
            total += qty * (2 * avg_entry - spot)
    return total


def _unrealized_pnl(side: str, eth_qty: float, avg_entry: float, spot: float) -> float:
    if eth_qty <= 0:
        return 0.0
    if side == "long":
        return eth_qty * (spot - avg_entry)
    return eth_qty * (avg_entry - spot)


def _open_eth_qty(suggestion: Suggestion, cash: float) -> float:
    """Convert validated USD notional size to asset qty, capped by cash/qty bounds."""
    entry = float(suggestion.entry)  # type: ignore[arg-type]
    size_usd = float(suggestion.size)
    product_id = getattr(suggestion, "product_id", None) or "ETH-USD"
    min_qty, max_qty = bot_config.qty_caps(product_id)
    if size_usd <= 0 or entry <= 0 or cash <= 0:
        return 0.0
    eth_qty = size_usd / entry
    max_affordable = cash / entry
    eth_qty = min(eth_qty, max_affordable, max_qty)
    if eth_qty < min_qty:
        return 0.0
    return eth_qty


def _signed_eth_qty(side: str, eth_qty: float) -> float:
    return eth_qty if side == "long" else -eth_qty


def _aggregate_signed_qty(positions: list[dict]) -> float:
    return sum(_signed_eth_qty(str(p["side"]), _pos_qty(p)) for p in positions)


def _close_all_positions(
    conn: sqlite3.Connection,
    cash: float,
    positions: list[dict],
    spot: float,
    cycle_id: str | None,
    reason: str,
    spots: dict[str, float] | None = None,
) -> float:
    resolved = spots or {"ETH-USD": float(spot)}
    for position in list(positions):
        cash = _close_position_at_market(
            conn, cash, position, spot, cycle_id, reason, spots=resolved
        )
    return cash


def _queue_outcome_chart(event: dict) -> None:
    _PENDING_OUTCOMES.append(event)


def flush_pending_outcome_charts() -> None:
    """Best-effort outcome PNG render for queued full closes (never raises)."""
    events = list(_PENDING_OUTCOMES)
    _PENDING_OUTCOMES.clear()
    for event in events:
        try:
            _render_outcome_chart(event)
        except Exception:
            logger.exception(
                "outcome chart failed for cycle %s",
                event.get("open_cycle_id"),
            )


def _render_outcome_chart(event: dict) -> None:
    """Build H4/M5 outcome charts for a fully closed paper position."""
    open_cycle_id = event.get("open_cycle_id")
    if not open_cycle_id:
        return

    import audit
    import charts as charts_mod
    import ledger
    import research
    from models import Suggestion

    row = ledger.get_suggestion_by_cycle_id(str(open_cycle_id))
    snapshot = audit.get_snapshot(str(open_cycle_id))
    suggestion_data = (snapshot or {}).get("suggestion") or {}

    action = str(
        (row or {}).get("action")
        or suggestion_data.get("action")
        or event.get("action")
        or ("spot_buy" if event.get("side") == "long" else "spot_sell")
    )
    entry = event.get("entry")
    if entry is None and row is not None:
        entry = row.get("entry")
    if entry is None:
        entry = suggestion_data.get("entry")

    stop = event.get("stop_loss")
    if stop is None and row is not None:
        stop = row.get("stop_loss")
    if stop is None:
        stop = suggestion_data.get("stop_loss")

    tps = event.get("take_profits") or []
    if not tps and row is not None:
        tps = row.get("take_profits") or []
    if not tps:
        tps = suggestion_data.get("take_profits") or []

    order_block = suggestion_data.get("order_block") or event.get("order_block")
    suggestion = Suggestion(
        action=action,
        size=float((row or {}).get("size") or suggestion_data.get("size") or 0),
        entry=float(entry) if entry is not None else None,
        stop_loss=float(stop) if stop is not None else None,
        take_profits=[float(tp) for tp in tps],
        risk_reward=(
            float(row["risk_reward"])
            if row and row.get("risk_reward") is not None
            else (
                float(suggestion_data["risk_reward"])
                if suggestion_data.get("risk_reward") is not None
                else None
            )
        ),
        rationale=str((row or {}).get("rationale") or suggestion_data.get("rationale") or ""),
        order_block=order_block,
        structure_chart=suggestion_data.get("structure_chart") or "H4",
        entry_chart=suggestion_data.get("entry_chart") or "M5",
        product_id=str(
            event.get("product_id")
            or (row or {}).get("product_id")
            or suggestion_data.get("product_id")
            or "ETH-USD"
        ),
    )

    key_levels = []
    htf_zones = []
    market_context = None
    snap_body = (snapshot or {}).get("snapshot")
    if isinstance(snap_body, dict):
        try:
            market_context = audit.market_context_from_dict(snap_body)
            key_levels = list(market_context.key_levels_near)
            htf_zones = list(market_context.htf_zones)
        except Exception:
            logger.debug("could not rebuild market context for outcome charts", exc_info=True)

    product_id = suggestion.product_id or "ETH-USD"
    data = {
        "H4": research.get_ohlc("H4", product_id=product_id),
        "H1": research.get_ohlc("H1", product_id=product_id),
        "M5": research.get_ohlc("M5", product_id=product_id),
    }
    charts_mod.build_outcome_charts(
        suggestion,
        data,
        key_levels,
        htf_zones,
        str(open_cycle_id),
        opened_at=event.get("opened_at"),
        closed_at=event.get("closed_at"),
        exit_price=float(event["exit"]),
        pnl_usd=float(event.get("pnl_usd") or 0),
        pnl_pct=float(event.get("pnl_pct") or 0),
        market_context=market_context,
    )


def _reduce_position(
    conn: sqlite3.Connection,
    cash: float,
    position: dict,
    close_qty: float,
    spot: float,
    cycle_id: str | None,
    reason: str,
    spots: dict[str, float] | None = None,
    *,
    allow_dust: bool = False,
) -> float:
    side = str(position["side"])
    eth_qty = _pos_qty(position)
    product_id = _pos_product(position)
    min_qty, _ = bot_config.qty_caps(product_id)
    close_qty = min(close_qty, eth_qty)
    if close_qty <= 0:
        return cash

    avg_entry = float(position["avg_entry"])
    pos_id = int(position["id"])
    resolved = spots or {product_id: float(spot)}

    if side == "long":
        cash += close_qty * spot
    else:
        cash += close_qty * (2 * avg_entry - spot)

    remaining = eth_qty - close_qty
    if remaining <= 1e-12:
        position["eth_qty"] = eth_qty
        position["qty"] = eth_qty
        return _close_position_at_market(
            conn, cash, position, spot, cycle_id, reason, spots=resolved
        )
    if not allow_dust and remaining < min_qty:
        position["eth_qty"] = eth_qty
        position["qty"] = eth_qty
        return _close_position_at_market(
            conn, cash, position, spot, cycle_id, reason, spots=resolved
        )

    conn.execute(
        "UPDATE paper_positions SET eth_qty = ?, qty = ? WHERE id = ?",
        (remaining, remaining, pos_id),
    )
    position["eth_qty"] = remaining
    position["qty"] = remaining
    open_positions = _fetch_open_positions(conn)
    equity = _equity(cash, open_positions, resolved)
    _log_trade(
        conn,
        "close",
        cycle_id,
        side,
        close_qty,
        spot,
        cash,
        equity,
        pos_id,
        reason,
        product_id=product_id,
    )
    return cash


def _reduce_positions_fifo(
    conn: sqlite3.Connection,
    cash: float,
    positions: list[dict],
    reduce_qty: float,
    spot: float,
    cycle_id: str | None,
    reason: str,
    spots: dict[str, float] | None = None,
) -> float:
    remaining = reduce_qty
    for position in positions:
        if remaining <= 0:
            break
        take = min(_pos_qty(position), remaining)
        cash = _reduce_position(
            conn, cash, position, take, spot, cycle_id, reason, spots=spots
        )
        remaining -= take
    return cash


def _tighter_stop(side: str, current: float, candidate: float) -> float:
    """Never widen risk: shorts keep the lower SL, longs keep the higher SL."""
    if side == "short":
        return min(current, candidate)
    return max(current, candidate)


def _update_position_metadata(
    conn: sqlite3.Connection,
    position: dict,
    suggestion: Suggestion,
    cycle_id: str | None,
) -> None:
    side = str(position["side"])
    stop = _tighter_stop(
        side,
        float(position["stop_loss"]),
        float(suggestion.stop_loss),  # type: ignore[arg-type]
    )
    conn.execute(
        """
        UPDATE paper_positions
        SET stop_loss = ?, take_profits = ?, risk_reward = ?, suggested_size = ?,
            action = ?, open_cycle_id = ?
        WHERE id = ?
        """,
        (
            stop,
            json.dumps(suggestion.take_profits),
            suggestion.risk_reward,
            suggestion.size,
            suggestion.action,
            cycle_id,
            int(position["id"]),
        ),
    )


def _add_to_net_position(
    conn: sqlite3.Connection,
    cash: float,
    position: dict,
    suggestion: Suggestion,
    add_qty: float,
    spot: float,
    cycle_id: str | None,
    spots: dict[str, float] | None = None,
) -> float:
    entry = float(suggestion.entry)  # type: ignore[arg-type]
    if add_qty <= 0:
        return cash

    product_id = _pos_product(position)
    min_qty, max_qty = bot_config.qty_caps(product_id)
    old_qty = _pos_qty(position)
    room = max_qty - old_qty
    if room < min_qty:
        return cash
    add_qty = min(add_qty, room)

    notional = add_qty * entry
    if cash < notional:
        return cash

    old_entry = float(position["avg_entry"])
    new_qty = old_qty + add_qty
    new_avg = (old_qty * old_entry + add_qty * entry) / new_qty
    side = str(position["side"])
    pos_id = int(position["id"])
    resolved = spots or {product_id: float(spot)}
    tranches = _merge_entry_tranches(
        _parse_entry_tranches(position.get("entry_tranches")),
        suggestion.entry_tranche,
    )
    stop = _tighter_stop(
        side,
        float(position["stop_loss"]),
        float(suggestion.stop_loss),  # type: ignore[arg-type]
    )
    # Keep the original OB link — never retarget SL/ref onto a different block.
    existing_ref = position.get("order_block_ref")
    keep_ref = existing_ref or suggestion.order_block_ref

    cash -= notional
    conn.execute(
        """
        UPDATE paper_positions
        SET eth_qty = ?, qty = ?, avg_entry = ?, stop_loss = ?, take_profits = ?,
            risk_reward = ?, suggested_size = ?, action = ?, open_cycle_id = ?,
            order_block_ref = ?,
            entry_tranches = ?
        WHERE id = ?
        """,
        (
            new_qty,
            new_qty,
            new_avg,
            stop,
            json.dumps(position.get("take_profits") or suggestion.take_profits),
            suggestion.risk_reward,
            suggestion.size,
            suggestion.action,
            cycle_id,
            keep_ref,
            json.dumps(tranches) if tranches else None,
            pos_id,
        ),
    )
    open_positions = _fetch_open_positions(conn)
    equity = _equity(cash, open_positions, resolved)
    _log_trade(
        conn,
        "open",
        cycle_id,
        side,
        add_qty,
        entry,
        cash,
        equity,
        pos_id,
        None,
        product_id=product_id,
    )
    return cash


def _match_position_for_add(
    same_side: list[dict],
    suggestion: Suggestion,
    spot: float | None = None,
) -> tuple[dict | None, bool]:
    """Pick the position to scale into, and say whether one was refused.

    Returns ``(position, refused)``. Two unrelated answers used to share the
    ``None`` slot: *no position holds this order block*, a genuinely new idea
    that should open its own position with its own stop; and *a position holds
    it but is underwater*, a scale-in ``SCALE_IN_MIN_R`` declines. The caller
    has to tell them apart, because opening a parallel clip on a refused add is
    the averaging-down the guard exists to prevent — and it lands with the
    newer, wider stop, so it adds size to a loser at worse risk than the leg
    already on.
    """
    ref = suggestion.order_block_ref
    if not ref:
        return (same_side[0] if same_side else None), False
    refused = False
    for pos in same_side:
        if str(pos.get("order_block_ref") or "") != ref:
            continue
        if spot is not None:
            unrealized_r = _unrealized_r(pos, float(spot))
            if unrealized_r is None or unrealized_r < bot_config.SCALE_IN_MIN_R:
                logger.info(
                    "Paper: scale-in refused, pos=%s underwater R=%.2f need >= %.2f",
                    pos.get("id"),
                    unrealized_r if unrealized_r is not None else float("nan"),
                    bot_config.SCALE_IN_MIN_R,
                )
                refused = True
                continue
        return pos, False
    return None, refused


def _unrealized_r(pos: dict, spot: float) -> float | None:
    try:
        entry = float(pos["avg_entry"])
        stop = float(pos["stop_loss"])
    except (KeyError, TypeError, ValueError):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    side = str(pos.get("side") or "")
    if side == "long":
        return (spot - entry) / risk
    if side == "short":
        return (entry - spot) / risk
    return None


def _apply_trade_with_netting(
    conn: sqlite3.Connection,
    cash: float,
    suggestion: Suggestion,
    spot: float,
    cycle_id: str | None,
    spots: dict[str, float] | None = None,
) -> float:
    """Reconcile incoming trade against open exposure (Option A: immediate net at spot)."""
    product_id = getattr(suggestion, "product_id", None) or "ETH-USD"
    min_qty, _ = bot_config.qty_caps(product_id)
    resolved = spots or {product_id: float(spot)}
    incoming_qty = _open_eth_qty(suggestion, cash)
    if incoming_qty <= 0:
        return cash

    incoming_signed = (
        incoming_qty if suggestion.action in LONG_ACTIONS else -incoming_qty
    )
    all_positions = _fetch_open_positions(conn)
    positions = [p for p in all_positions if _pos_product(p) == product_id]
    current_signed = _aggregate_signed_qty(positions)
    target_signed = current_signed + incoming_signed

    if not positions:
        return _open_position(
            conn, cash, suggestion, spot, cycle_id, spots=resolved
        )

    if abs(target_signed) < min_qty:
        return _close_all_positions(
            conn, cash, positions, spot, cycle_id, "signal_net", spots=resolved
        )

    if target_signed == 0:
        return _close_all_positions(
            conn, cash, positions, spot, cycle_id, "signal_net", spots=resolved
        )

    current_side = "long" if current_signed > 0 else "short"
    target_side = "long" if target_signed > 0 else "short"

    if target_side != current_side:
        cash = _close_all_positions(
            conn, cash, positions, spot, cycle_id, "signal_net", spots=resolved
        )
        return _open_position(
            conn,
            cash,
            suggestion,
            spot,
            cycle_id,
            eth_qty_override=abs(target_signed),
            spots=resolved,
        )

    if abs(target_signed) > abs(current_signed):
        add_qty = abs(target_signed) - abs(current_signed)
        same_side = [p for p in positions if str(p["side"]) == current_side]
        if not same_side:
            return _open_position(
                conn,
                cash,
                suggestion,
                spot,
                cycle_id,
                eth_qty_override=abs(target_signed),
                spots=resolved,
            )
        target_pos, refused = _match_position_for_add(
            same_side, suggestion, spot=spot
        )
        if refused:
            # The guard declined this add. Opening a parallel clip instead would
            # add the same size to the same losing idea at a wider stop, which
            # is what the guard is for.
            return cash
        if target_pos is None:
            # Different OB / new idea — open a separate position with its own SL.
            return _open_position(
                conn,
                cash,
                suggestion,
                spot,
                cycle_id,
                eth_qty_override=add_qty,
                spots=resolved,
            )
        return _add_to_net_position(
            conn,
            cash,
            target_pos,
            suggestion,
            add_qty,
            spot,
            cycle_id,
            spots=resolved,
        )

    if abs(target_signed) < abs(current_signed):
        reduce_qty = abs(current_signed) - abs(target_signed)
        same_side = [p for p in positions if str(p["side"]) == current_side]
        return _reduce_positions_fifo(
            conn,
            cash,
            same_side,
            reduce_qty,
            spot,
            cycle_id,
            "signal_net",
            spots=resolved,
        )

    _update_position_metadata(conn, positions[0], suggestion, cycle_id)
    return cash

def _parse_take_profits(raw: str | list | None) -> list[float]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [float(tp) for tp in raw]
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [float(tp) for tp in values]


def _row_to_position(row: sqlite3.Row | dict) -> dict:
    pos = dict(row)
    pos["take_profits"] = _parse_take_profits(pos.get("take_profits"))
    pos["entry_tranches"] = _parse_entry_tranches(pos.get("entry_tranches"))
    product_id = str(pos.get("product_id") or "ETH-USD")
    pos["product_id"] = product_id
    qty = pos.get("qty")
    if qty is None:
        qty = pos.get("eth_qty") or 0
    qty_f = float(qty)
    pos["qty"] = qty_f
    pos["eth_qty"] = qty_f
    return pos


def _fetch_open_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM paper_positions
        WHERE status = 'open'
        ORDER BY opened_at ASC, id ASC
        """
    ).fetchall()
    return [_row_to_position(row) for row in rows]


def get_open_positions(
    spot_price: float | None = None,
    spots: dict[str, float] | None = None,
) -> list[dict]:
    """Return all open paper positions enriched with spot and unrealized P&L."""
    init_db()
    resolved = _resolve_spots(spot_price, spots)
    if not resolved:
        state = get_state()
        last = state.get("last_spot")
        if last is not None and float(last) > 0:
            resolved = {"ETH-USD": float(last)}

    with _connect() as conn:
        positions = _fetch_open_positions(conn)

    starting = float(get_state()["starting_usd"])
    enriched: list[dict] = []
    for pos in positions:
        side = str(pos["side"])
        eth_qty = _pos_qty(pos)
        avg_entry = float(pos["avg_entry"])
        product_id = _pos_product(pos)
        spot_f = _spot_for(product_id, resolved)
        if spot_f <= 0:
            spot_f = avg_entry
        unrealized = _unrealized_pnl(side, eth_qty, avg_entry, spot_f)
        enriched.append(
            {
                **pos,
                "product_id": product_id,
                "qty": eth_qty,
                "eth_qty": eth_qty,
                "spot": spot_f,
                "unrealized_pnl_usd": unrealized,
                "starting_usd": starting,
            }
        )
    return enriched


def get_state() -> dict:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone()
        positions = _fetch_open_positions(conn)
    state = dict(row)
    state["open_positions"] = positions
    state["open_count"] = len(positions)
    # Backward-compat fields from first open position (if any).
    if positions:
        first = positions[0]
        state["side"] = first["side"]
        state["eth_qty"] = first["eth_qty"]
        state["qty"] = first.get("qty", first["eth_qty"])
        state["product_id"] = first.get("product_id", "ETH-USD")
        state["avg_entry"] = first["avg_entry"]
        state["action"] = first.get("action")
        state["stop_loss"] = first.get("stop_loss")
        state["take_profits"] = first.get("take_profits")
        state["risk_reward"] = first.get("risk_reward")
        state["suggested_size"] = first.get("suggested_size")
        state["opened_at"] = first.get("opened_at")
        state["open_cycle_id"] = first.get("open_cycle_id")
    else:
        state["side"] = "flat"
        state["eth_qty"] = 0.0
        state["qty"] = 0.0
        state["product_id"] = None
        state["avg_entry"] = None
        state["action"] = None
        state["stop_loss"] = None
        state["take_profits"] = []
        state["risk_reward"] = None
        state["suggested_size"] = None
        state["opened_at"] = None
        state["open_cycle_id"] = None
    return state


def is_open(state: dict | None = None) -> bool:
    state = state or get_state()
    return int(state.get("open_count") or 0) > 0


def get_open_position(
    spot_price: float | None = None,
    spots: dict[str, float] | None = None,
) -> dict | None:
    """Return the oldest open position, or None if flat."""
    positions = get_open_positions(spot_price, spots=spots)
    if not positions:
        return None
    pos = positions[0]
    starting = float(pos["starting_usd"])
    cash = float(get_state()["cash_usd"])
    resolved = _resolve_spots(spot_price, spots)
    for p in positions:
        pid = _pos_product(p)
        if _spot_for(pid, resolved) <= 0:
            resolved[pid] = float(p["spot"])
    equity = _equity(cash, positions, resolved)
    return {
        **pos,
        "equity_usd": equity,
        "portfolio_pnl_usd": equity - starting,
        "portfolio_pnl_pct": ((equity - starting) / starting * 100) if starting else 0.0,
    }


def _format_exit_plan(position: dict) -> str:
    side = str(position["side"])
    sl = position.get("stop_loss")
    tps = position.get("take_profits") or []
    spot = float(position["spot"])

    lines: list[str] = []
    if sl is not None:
        sl_f = float(sl)
        if side == "short":
            lines.append(
                f"Stop loss at ${sl_f:,.2f} — exit if price rises above SL "
                f"(currently {'above' if spot >= sl_f else 'below'} spot)."
            )
        else:
            lines.append(
                f"Stop loss at ${sl_f:,.2f} — exit if price falls below SL "
                f"(currently {'below' if spot <= sl_f else 'above'} spot)."
            )

    for idx, tp in enumerate(tps, start=1):
        tp_f = float(tp)
        if side == "short":
            status = "hit" if spot <= tp_f else "pending"
            lines.append(
                f"TP{idx} at ${tp_f:,.2f} — scale out ~1/{max(len(tps), 1)} on downside ({status})."
            )
        else:
            status = "hit" if spot >= tp_f else "pending"
            lines.append(
                f"TP{idx} at ${tp_f:,.2f} — scale out ~1/{max(len(tps), 1)} on upside ({status})."
            )
    if len(tps) >= 2:
        lines.append(
            "After each target fills, the stop trails to that target so the runner locks it."
        )

    if not lines:
        return "No SL/TP levels recorded for this position."
    return "\n".join(lines)


def _format_single_position(position: dict, index: int | None = None) -> str:
    side = str(position["side"])
    action = str(position.get("action") or side).upper()
    eth_qty = _pos_qty(position)
    entry = float(position["avg_entry"])
    spot = float(position["spot"])
    unrealized = float(position["unrealized_pnl_usd"])
    sign = "+" if unrealized >= 0 else ""
    label_asset = bot_config.product_label(_pos_product(position))

    label = f"Long {label_asset}" if side == "long" else f"Short {label_asset}"
    prefix = f"Position {index}: " if index is not None else "Open position: "
    lines = [
        f"{prefix}{action} ({label})",
        f"Entered: {position.get('opened_at') or 'unknown'} (cycle {position.get('open_cycle_id') or 'n/a'})",
        f"Size: ${eth_qty * entry:,.2f} ({eth_qty:.4f} {label_asset})",
    ]
    if position.get("suggested_size") is not None:
        lines[-1] += f" (suggested ${float(position['suggested_size']):,.2f})"
    lines.extend(
        [
            f"Entry: ${entry:,.2f}",
            f"Current: ${spot:,.2f}",
            f"Unrealized P&L: {sign}${abs(unrealized):,.2f}",
        ]
    )
    if position.get("stop_loss") is not None:
        lines.append(f"Stop loss: ${float(position['stop_loss']):,.2f}")
    tps = position.get("take_profits") or []
    if tps:
        tp_str = ", ".join(f"${float(tp):,.2f}" for tp in tps)
        lines.append(f"Take profits: {tp_str}")
    if position.get("risk_reward") is not None:
        lines.append(f"R/R: {float(position['risk_reward']):.2f}")
    lines.append("Exit plan:")
    lines.append(_format_exit_plan(position))
    return "\n".join(lines)


def format_positions_detail(spot_price: float | None = None) -> str | None:
    """Multi-line breakdown of all open paper positions, or None if flat."""
    positions = get_open_positions(spot_price)
    if not positions:
        return None
    blocks = []
    for idx, pos in enumerate(positions, start=1):
        blocks.append(_format_single_position(pos, index=idx if len(positions) > 1 else None))
    return "\n\n".join(blocks)


def format_position_detail(spot_price: float | None = None) -> str | None:
    """Alias for format_positions_detail (backward compatible)."""
    return format_positions_detail(spot_price)


def _pair_closed_trades(rows: list[dict]) -> list[dict]:
    """Pair open/close ledger rows into closed trade summaries (oldest-first input).

    Supports partial closes (TP scale-outs / signal nets) and scale-ins: one
    close may span multiple open tranches, so it consumes across as many
    pending opens as its quantity covers (LIFO within its position, then any
    same-side open) and reports a single trade at the blended entry. Dropping
    the unmatched remainder instead — the old behavior — silently discarded
    realized P&L on multi-tranche positions and skewed win/loss stats.
    """
    pending_opens: dict[int | None, list[dict]] = {}
    closed: list[dict] = []

    def _open_remaining(opened: dict) -> float:
        if opened.get("_remaining") is not None:
            return float(opened["_remaining"])
        return float(
            opened.get("qty") if opened.get("qty") is not None else opened["eth_qty"]
        )

    def _next_open(pos_id: int | None, side: str) -> dict | None:
        opens = pending_opens.get(pos_id) or []
        for i in range(len(opens) - 1, -1, -1):
            if _open_remaining(opens[i]) > 1e-12:
                return opens[i]
        for other in pending_opens.values():
            for i in range(len(other) - 1, -1, -1):
                cand = other[i]
                if str(cand.get("side") or "") == side and _open_remaining(cand) > 1e-12:
                    return cand
        return None

    for row in rows:
        event = str(row.get("event") or "")
        pos_id = row.get("position_id")
        if event == "open":
            opened = dict(row)
            opened["_remaining"] = _open_remaining(opened)
            pending_opens.setdefault(pos_id, []).append(opened)
            continue
        if event != "close":
            continue

        side = str(row.get("side") or "")
        close_qty = float(row.get("qty") if row.get("qty") is not None else row["eth_qty"])

        need = close_qty
        consumed: list[tuple[float, dict]] = []
        while need > 1e-12:
            opened = _next_open(pos_id, side)
            if opened is None:
                break
            take = min(need, _open_remaining(opened))
            if take <= 1e-12:
                break
            opened["_remaining"] = _open_remaining(opened) - take
            consumed.append((take, opened))
            need -= take
        if not consumed:
            continue

        qty = sum(take for take, _ in consumed)
        entry = sum(take * float(o["price"]) for take, o in consumed) / qty
        primary = consumed[0][1]
        exit_price = float(row["price"])
        if side == "long":
            realized_pnl = qty * (exit_price - entry)
        else:
            realized_pnl = qty * (entry - exit_price)
        notional = qty * entry
        closed.append(
            {
                "side": side,
                "open_cycle_id": primary.get("cycle_id"),
                "close_cycle_id": row.get("cycle_id"),
                "eth_qty": qty,
                "qty": qty,
                "product_id": str(
                    row.get("product_id")
                    or primary.get("product_id")
                    or "ETH-USD"
                ),
                "entry": entry,
                "exit": exit_price,
                "opened_at": primary.get("ts"),
                "closed_at": row.get("ts"),
                "close_reason": row.get("close_reason"),
                "realized_pnl_usd": realized_pnl,
                "realized_pnl_pct": (realized_pnl / notional * 100) if notional else 0.0,
                "epoch_label": row.get("epoch_label"),
            }
        )

    return closed


def get_epoch_info() -> dict:
    """Current paper epoch metadata for dashboard / status."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone()
        archive_count = conn.execute(
            "SELECT COUNT(*) FROM paper_epochs"
        ).fetchone()[0]
    state = dict(row) if row else {}
    return {
        "starting_usd": float(state.get("starting_usd") or 0),
        "epoch_started_at": state.get("epoch_started_at"),
        "epoch_label": state.get("epoch_label") or bot_config.PAPER_EPOCH_LABEL,
        "prior_epoch_count": int(archive_count),
    }


def get_closed_trades(limit: int = 10) -> list[dict]:
    """Pair open/close rows from paper_trades; return most recent closed trades first."""
    init_db()
    with _connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM paper_trades ORDER BY id ASC"
            ).fetchall()
        ]

    closed = _pair_closed_trades(rows)
    closed.reverse()
    return closed[:limit]


def get_archived_book_summary() -> dict:
    """P&L stats for the archived paper epoch (v1), independent of the live book."""
    init_db()
    with _connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM paper_trades_archive ORDER BY id ASC"
            ).fetchall()
        ]
        epoch = conn.execute(
            "SELECT * FROM paper_epochs ORDER BY id ASC LIMIT 1"
        ).fetchone()
    closed = _pair_closed_trades(rows)
    epoch_d = dict(epoch) if epoch else {}
    starting = float(epoch_d.get("starting_usd") or 0)
    realized = sum(float(t.get("realized_pnl_usd") or 0) for t in closed)
    win_pnls = [
        float(t.get("realized_pnl_usd") or 0)
        for t in closed
        if float(t.get("realized_pnl_usd") or 0) > 0
    ]
    loss_pnls = [
        float(t.get("realized_pnl_usd") or 0)
        for t in closed
        if float(t.get("realized_pnl_usd") or 0) < 0
    ]
    n = len(closed)
    loss_abs = abs(sum(loss_pnls))
    return {
        "available": bool(rows or epoch),
        "epoch_label": epoch_d.get("label") or "v1",
        "starting_usd": starting,
        "ended_at": epoch_d.get("ended_at"),
        "closed_trade_count": n,
        "win_rate_pct": round(len(win_pnls) / n * 100, 1) if n else 0.0,
        "realized_pnl_usd": round(realized, 2),
        "realized_pnl_pct": round(realized / starting * 100, 2) if starting else 0.0,
        "avg_pnl_usd": round(realized / n, 2) if n else None,
        "avg_win_usd": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else None,
        "avg_loss_usd": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else None,
        "profit_factor": round(sum(win_pnls) / loss_abs, 2) if loss_abs else None,
    }


def get_archived_closed_trades(limit: int = 50) -> list[dict]:
    """Closed trades from archived epochs (most recent archive epoch first)."""
    init_db()
    with _connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM paper_trades_archive
                ORDER BY epoch_label DESC, id ASC
                """
            ).fetchall()
        ]

    closed = _pair_closed_trades(rows)
    closed.reverse()
    return closed[:limit]


def format_closed_trades_detail(limit: int = 5) -> str | None:
    """Format recent closed paper trades with realized P&L, or None if none."""
    trades = get_closed_trades(limit=limit)
    if not trades:
        return None

    try:
        import ledger
    except ImportError:
        ledger = None  # type: ignore[assignment]

    lines = ["Closed paper trades (most recent first):"]
    for idx, trade in enumerate(trades, start=1):
        side = str(trade["side"])
        action = "spot_buy" if side == "long" else "deriv_sell"
        open_cycle_id = trade.get("open_cycle_id")
        if ledger and open_cycle_id:
            row = ledger.get_suggestion_by_cycle_id(str(open_cycle_id))
            if row and row.get("action"):
                action = str(row["action"])

        pnl = float(trade["realized_pnl_usd"])
        pnl_pct = float(trade["realized_pnl_pct"])
        if pnl >= 0:
            pnl_str = f"+${pnl:,.2f} (+{pnl_pct:.2f}%)"
        else:
            pnl_str = f"-${abs(pnl):,.2f} ({pnl_pct:.2f}%)"
        reason = trade.get("close_reason") or "market"
        asset = bot_config.product_label(
            str(trade.get("product_id") or "ETH-USD")
        )
        lines.append(
            f"{idx}. {action.upper()} {float(trade['eth_qty']):.4f} {asset} "
            f"@ ${float(trade['entry']):,.2f} -> ${float(trade['exit']):,.2f} "
            f"| realized {pnl_str} | closed via {reason} "
            f"| opened {trade.get('opened_at')} (cycle {open_cycle_id}) "
            f"| closed {trade.get('closed_at')}"
        )

    return "\n".join(lines)


def _log_trade(
    conn: sqlite3.Connection,
    event: str,
    cycle_id: str | None,
    side: str | None,
    eth_qty: float,
    price: float,
    cash: float,
    equity: float,
    position_id: int | None = None,
    close_reason: str | None = None,
    product_id: str | None = None,
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pid = product_id or "ETH-USD"
    conn.execute(
        """
        INSERT INTO paper_trades (
            ts, cycle_id, event, side, eth_qty, qty, product_id, price, cash_usd, equity_usd,
            position_id, close_reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            cycle_id,
            event,
            side,
            eth_qty,
            eth_qty,
            pid,
            price,
            cash,
            equity,
            position_id,
            close_reason,
        ),
    )


def _close_position_at_market(
    conn: sqlite3.Connection,
    cash: float,
    position: dict,
    spot: float,
    cycle_id: str | None,
    reason: str,
    spots: dict[str, float] | None = None,
) -> float:
    side = str(position["side"])
    eth_qty = _pos_qty(position)
    avg_entry = float(position["avg_entry"])
    pos_id = int(position["id"])
    product_id = _pos_product(position)
    resolved = spots or {product_id: float(spot)}

    if side == "long":
        cash += eth_qty * spot
        pnl_usd = eth_qty * (spot - avg_entry)
    elif side == "short":
        cash += eth_qty * (2 * avg_entry - spot)
        pnl_usd = eth_qty * (avg_entry - spot)
    else:
        pnl_usd = 0.0

    open_positions = [p for p in _fetch_open_positions(conn) if int(p["id"]) != pos_id]
    equity = _equity(cash, open_positions, resolved)
    conn.execute(
        "UPDATE paper_positions SET status = 'closed' WHERE id = ?",
        (pos_id,),
    )
    closed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _log_trade(
        conn,
        "close",
        cycle_id,
        side,
        eth_qty,
        spot,
        cash,
        equity,
        pos_id,
        reason or "market",
        product_id=product_id,
    )
    notional = eth_qty * avg_entry
    _queue_outcome_chart(
        {
            "open_cycle_id": position.get("open_cycle_id"),
            "opened_at": position.get("opened_at"),
            "closed_at": closed_at,
            "side": side,
            "action": position.get("action"),
            "product_id": product_id,
            "entry": avg_entry,
            "exit": spot,
            "eth_qty": eth_qty,
            "qty": eth_qty,
            "stop_loss": position.get("stop_loss"),
            "take_profits": list(position.get("take_profits") or []),
            "close_reason": reason or "market",
            "pnl_usd": pnl_usd,
            "pnl_pct": (pnl_usd / notional * 100) if notional else 0.0,
            "mfe_pct": position.get("mfe_pct"),
            "mae_pct": position.get("mae_pct"),
        }
    )
    return cash


def _sl_hit(side: str, spot: float, stop_loss: float) -> bool:
    if side == "long":
        return spot <= stop_loss
    return spot >= stop_loss


def _m5_path(product_id: str, since: str | None) -> list[dict]:
    """M5 bars between the last barrier walk and now, oldest first.

    Returns empty when the window is unknown or the fetch fails, which
    collapses the caller back to a spot-only check. A missing candle feed must
    not stall the cycle, but it does mean a stop can still be missed, so the
    failure is logged rather than swallowed silently.
    """
    if not since:
        return []
    try:
        start = int(
            datetime.fromisoformat(str(since).replace("Z", "+00:00")).timestamp()
        )
    except ValueError:
        return []
    end = int(datetime.now(timezone.utc).timestamp())
    if start >= end:
        return []
    try:
        import research

        return research.fetch_coinbase_candles_range(
            "FIVE_MINUTE", start, end, product_id=product_id
        )
    except Exception:
        logger.warning(
            "paper: M5 path unavailable for %s since %s; "
            "falling back to spot-only barriers",
            product_id,
            since,
            exc_info=True,
        )
        return []


def _entry_is_fillable(side: str, entry: float, price: float) -> bool:
    """Whether a resting limit at ``entry`` would fill at ``price``.

    Eva's entries are pullback limits into an M5 order block, routinely a
    percent below a long's current price. Booking one before price gets there
    invents a fill the market never offered, and when the plan's first target
    also sits behind spot the position banks an instant win it never earned —
    which is exactly what BTC cycle 20260902T181231Z did.
    """
    if entry <= 0 or price <= 0:
        return True
    return price <= entry if side == "long" else price >= entry


def _entry_touched(
    side: str, entry: float, product_id: str, since: str | None, spot: float
) -> bool:
    """Did price trade to a resting limit since the last check?

    A long limit fills on the way down and a short's on the way up, which is
    the same extreme the stop watches, so the probe's adverse leg is already
    the right price to test.
    """
    return any(
        _entry_is_fillable(side, entry, adverse)
        for adverse, _ in _barrier_probes(side, product_id, since, spot)
    )


def _barrier_probes(
    side: str, product_id: str, since: str | None, spot: float
) -> list[tuple[float, float]]:
    """(adverse, favourable) prices to test in order, oldest bar first.

    Each M5 bar contributes its two extremes and the live spot closes the
    sequence. The caller tests the adverse price first, which is what makes a
    bar that spans both barriers resolve stop-first.
    """
    probes: list[tuple[float, float]] = []
    for bar in _m5_path(product_id, since):
        try:
            low = float(bar["low"])
            high = float(bar["high"])
        except (KeyError, TypeError, ValueError):
            continue
        probes.append((low, high) if side == "long" else (high, low))
    probes.append((spot, spot))
    return probes


def _ordered_take_profits(side: str, take_profits: list[float]) -> list[float]:
    """Nearest-first TP ladder (long: ascending, short: descending)."""
    if side == "long":
        return sorted(float(tp) for tp in take_profits)
    return sorted((float(tp) for tp in take_profits), reverse=True)


def _tp_level_hit(side: str, spot: float, tp: float) -> bool:
    if side == "long":
        return spot >= tp
    return spot <= tp


def _sl_after_tp_hit(
    side: str,
    avg_entry: float,
    ordered_tps: list[float],
    tps_hit_after: int,
) -> float:
    """Trail one rung behind: after TP1 → breakeven; after TP2 → TP1.

    Matches ``execute._trailed_stop`` so the two books remain comparable, and
    matches the exit rule the stop study measures Eva under.
    """
    if tps_hit_after <= 0 or not ordered_tps or tps_hit_after == 1:
        return float(avg_entry)
    idx = min(tps_hit_after - 2, len(ordered_tps) - 1)
    return float(ordered_tps[idx])


def _update_excursions(
    conn: sqlite3.Connection,
    position: dict,
    spot: float,
) -> None:
    """Track max favorable / adverse excursion (% of entry) while position is open."""
    entry = float(position["avg_entry"])
    if entry <= 0:
        return
    side = str(position["side"])
    if side == "long":
        fav = (spot - entry) / entry * 100.0
        adv = (entry - spot) / entry * 100.0
    else:
        fav = (entry - spot) / entry * 100.0
        adv = (spot - entry) / entry * 100.0
    prev_mfe = position.get("mfe_pct")
    prev_mae = position.get("mae_pct")
    mfe = max(float(prev_mfe), fav) if prev_mfe is not None else fav
    mae = max(float(prev_mae), adv) if prev_mae is not None else adv
    conn.execute(
        "UPDATE paper_positions SET mfe_pct = ?, mae_pct = ? WHERE id = ?",
        (mfe, mae, int(position["id"])),
    )
    position["mfe_pct"] = mfe
    position["mae_pct"] = mae


def tighten_stops_from_pulse(
    *,
    recommendation: str,
    spots: dict[str, float] | None = None,
    event_id: int | None = None,
) -> list[dict]:
    """Mechanically ratchet house stops when a macro pulse says tighten_sl.

    Moves stop halfway from current stop toward entry (never widens). Returns
    list of {position_id, old_sl, new_sl} for applied changes.
    """
    if str(recommendation or "").strip() != "tighten_sl":
        return []
    init_db()
    resolved = spots or {}
    if not resolved:
        try:
            import research

            resolved = research.get_spot_prices()
        except Exception:
            resolved = {}
    applied: list[dict] = []
    with _connect() as conn:
        for position in list(_fetch_open_positions(conn)):
            side = str(position["side"])
            entry = float(position["avg_entry"])
            old_sl = float(position["stop_loss"])
            # Midpoint of entry↔current SL — tighter risk, never widens.
            mid = round((entry + old_sl) / 2.0, 2)
            if side == "long":
                new_sl = max(old_sl, mid)
                if new_sl <= old_sl:
                    continue
            else:
                new_sl = min(old_sl, mid)
                if new_sl >= old_sl:
                    continue
            conn.execute(
                "UPDATE paper_positions SET stop_loss = ? WHERE id = ?",
                (new_sl, int(position["id"])),
            )
            applied.append(
                {
                    "position_id": int(position["id"]),
                    "product_id": _pos_product(position),
                    "side": side,
                    "old_sl": old_sl,
                    "new_sl": new_sl,
                    "event_id": event_id,
                    "source": "macro_pulse_tighten_sl",
                }
            )
            logger.info(
                "Macro pulse tightened SL pos=%s %s %s → %s",
                position["id"],
                side,
                old_sl,
                new_sl,
            )
        conn.commit()
    return applied


def _check_sl_tp_closes(
    conn: sqlite3.Connection,
    cash: float,
    spots: dict[str, float],
    cycle_id: str | None,
) -> float:
    """SL full exit; staged TP scale-out (1/N remaining levels) with SL trail.

    Barriers resolve on the M5 path walked since the previous cycle, not on
    spot at poll time. Sampling spot alone let a position survive a stop that
    traded through between polls and book as a win when price later recovered
    to a target — three of Eva HQ's nine recorded winners, which is the
    difference between a reported +0.882R and a true +0.346R. Live brackets
    fill intrabar; this makes paper answer the same question.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for position in list(_fetch_open_positions(conn)):
        side = str(position["side"])
        pos_id = int(position["id"])
        product_id = _pos_product(position)
        spot = _spot_for(product_id, spots)
        if spot <= 0:
            continue
        _update_excursions(conn, position, spot)

        probes = _barrier_probes(
            side, product_id, position.get("path_checked_at"), spot
        )
        closed = False
        for adverse, favourable in probes:
            if position is None:
                closed = True
                break
            # Re-read: a rung filled on an earlier bar trails the stop.
            sl = float(position["stop_loss"])
            if _sl_hit(side, adverse, sl):
                cash = _close_position_at_market(
                    conn, cash, position, sl, cycle_id, "stop_loss", spots=spots
                )
                closed = True
                break
            cash, position, closed = _fill_reached_targets(
                conn, cash, position, favourable, cycle_id, spots
            )
            if closed:
                break

        if not closed:
            conn.execute(
                "UPDATE paper_positions SET path_checked_at = ? WHERE id = ?",
                (now, pos_id),
            )
    return cash


def _fill_reached_targets(
    conn: sqlite3.Connection,
    cash: float,
    position: dict,
    price: float,
    cycle_id: str | None,
    spots: dict[str, float],
) -> tuple[float, dict | None, bool]:
    """Fill every ladder rung ``price`` has reached, trailing the stop behind.

    Gap-through: one bar can clear several rungs, so this drains the ladder
    rather than advancing a single step. Returns the refreshed position and
    whether the remainder is now out.
    """
    side = str(position["side"])
    ordered_tps = _ordered_take_profits(side, position.get("take_profits") or [])
    if not ordered_tps:
        return cash, position, False

    pos_id = int(position["id"])
    avg_entry = float(position["avg_entry"])
    tps_hit = int(position.get("tps_hit") or 0)

    while tps_hit < len(ordered_tps):
        # Re-read qty/SL in case a prior partial updated the row.
        live = next(
            (p for p in _fetch_open_positions(conn) if int(p["id"]) == pos_id),
            None,
        )
        if live is None:
            return cash, None, True
        position = live
        eth_qty = _pos_qty(position)
        if eth_qty <= 0:
            return cash, position, False

        tp_price = ordered_tps[tps_hit]
        if not _tp_level_hit(side, price, tp_price):
            return cash, position, False

        remaining_levels = len(ordered_tps) - tps_hit
        if remaining_levels <= 1:
            cash = _close_position_at_market(
                conn, cash, position, tp_price, cycle_id, "take_profit", spots=spots
            )
            return cash, None, True

        close_qty = eth_qty / remaining_levels
        cash = _reduce_position(
            conn,
            cash,
            position,
            close_qty,
            tp_price,
            cycle_id,
            "take_profit",
            spots=spots,
            allow_dust=True,
        )
        live = next(
            (p for p in _fetch_open_positions(conn) if int(p["id"]) == pos_id),
            None,
        )
        if live is None:
            return cash, None, True

        tps_hit += 1
        new_sl = _tighter_stop(
            side,
            float(live["stop_loss"]),
            _sl_after_tp_hit(side, avg_entry, ordered_tps, tps_hit),
        )
        conn.execute(
            "UPDATE paper_positions SET stop_loss = ?, tps_hit = ? WHERE id = ?",
            (new_sl, tps_hit, pos_id),
        )
        position = next(
            (p for p in _fetch_open_positions(conn) if int(p["id"]) == pos_id),
            position,
        )

    return cash, position, False


def _record_pending_entry(
    conn: sqlite3.Connection,
    suggestion: Suggestion,
    eth_qty: float,
    cycle_id: str | None,
    product_id: str,
    side: str,
    now: str,
) -> None:
    """Park a plan whose limit price has not been reached.

    Held as a ``pending`` row rather than a separate table so it carries the
    whole plan and stays invisible to every reader — they all filter on
    ``status = 'open'``. No cash moves until it fills.
    """
    tranches = _merge_entry_tranches([], suggestion.entry_tranche)
    conn.execute(
        """
        INSERT INTO paper_positions (
            open_cycle_id, opened_at, side, action, eth_qty, qty, product_id, avg_entry,
            stop_loss, take_profits, risk_reward, suggested_size, status,
            order_block_ref, entry_tranches, path_checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            cycle_id,
            now,
            side,
            suggestion.action,
            eth_qty,
            eth_qty,
            product_id,
            float(suggestion.entry),  # type: ignore[arg-type]
            float(suggestion.stop_loss),  # type: ignore[arg-type]
            json.dumps(suggestion.take_profits),
            suggestion.risk_reward,
            suggestion.size,
            suggestion.order_block_ref,
            json.dumps(tranches) if tranches else None,
            now,
        ),
    )


def _cancel_pending_entry(conn: sqlite3.Connection, product_id: str) -> None:
    """Drop a waiting plan once Eva re-reads the chart and declines it.

    A plan is only valid while the structure that justified it is. Every cycle
    evaluates every traded product, so a ``no_trade`` is a fresh verdict on the
    same chart rather than silence — resting the old limit through it would
    hold an order Eva would no longer write.

    This, not the fixed clock, is what actually bounds a pending's life. Eva
    re-issues a plan for a given product about 65% of the time (n=855
    suggestions since 2026-08-15, all lanes), so a plan survives ~2.9 cycles on
    average, roughly 1.4h at a 30-minute cadence. That lands in the region the
    limit-fill sweep measured as least adverse without being tuned to it: the
    sweep's P&L is non-monotonic across expiries and must not be fitted, but
    its fill rate rises monotonically with the window, so short is defensible
    on the mechanism alone.
    """
    cur = conn.execute(
        "DELETE FROM paper_positions WHERE status = 'pending' AND product_id = ?",
        (product_id,),
    )
    if cur.rowcount:
        logger.info(
            "paper: %s pending entry cancelled — no setup this cycle", product_id
        )


def _fetch_pending_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM paper_positions
        WHERE status = 'pending'
        ORDER BY opened_at ASC, id ASC
        """
    ).fetchall()
    return [_row_to_position(row) for row in rows]


def _fill_pending_entry(
    conn: sqlite3.Connection,
    cash: float,
    position: dict,
    cycle_id: str | None,
    spots: dict[str, float],
    now: str,
) -> float:
    """Turn a touched plan into a real position at its limit price."""
    pos_id = int(position["id"])
    qty = _pos_qty(position)
    entry = float(position["avg_entry"])
    notional = qty * entry
    if qty <= 0 or entry <= 0 or notional > cash:
        conn.execute("DELETE FROM paper_positions WHERE id = ?", (pos_id,))
        return cash

    cash -= notional
    # The barrier walk starts at the fill, not at the plan: bars before the
    # entry was touched belong to a position that did not exist yet.
    conn.execute(
        """
        UPDATE paper_positions
        SET status = 'open', opened_at = ?, path_checked_at = ?
        WHERE id = ?
        """,
        (now, now, pos_id),
    )
    equity = _equity(cash, _fetch_open_positions(conn), spots)
    _log_trade(
        conn,
        "open",
        cycle_id,
        str(position["side"]),
        qty,
        entry,
        cash,
        equity,
        pos_id,
        None,
        product_id=_pos_product(position),
    )
    return cash


def _fill_pending_entries(
    conn: sqlite3.Connection,
    cash: float,
    spots: dict[str, float],
    cycle_id: str | None,
) -> float:
    """Open plans whose entry price traded, and drop the ones that went stale."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    ttl_hours = max(float(bot_config.PAPER_PENDING_EXPIRY_HOURS or 0), 0.0)

    for position in _fetch_pending_positions(conn):
        pos_id = int(position["id"])
        side = str(position["side"])
        product_id = _pos_product(position)
        entry = float(position["avg_entry"])
        spot = _spot_for(product_id, spots)
        if spot <= 0:
            continue

        if _entry_touched(
            side, entry, product_id, position.get("path_checked_at"), spot
        ):
            cash = _fill_pending_entry(conn, cash, position, cycle_id, spots, now)
            continue

        if ttl_hours and _hours_since(position.get("opened_at"), now_dt) >= ttl_hours:
            conn.execute("DELETE FROM paper_positions WHERE id = ?", (pos_id,))
            logger.info(
                "paper: %s %s limit %.2f expired unfilled after %.1fh",
                product_id,
                side,
                entry,
                ttl_hours,
            )
            continue

        conn.execute(
            "UPDATE paper_positions SET path_checked_at = ? WHERE id = ?",
            (now, pos_id),
        )
    return cash


def _hours_since(ts: str | None, now: datetime) -> float:
    if not ts:
        return 0.0
    try:
        started = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (now - started).total_seconds() / 3600.0


def _open_position(
    conn: sqlite3.Connection,
    cash: float,
    suggestion: Suggestion,
    spot: float,
    cycle_id: str | None,
    *,
    eth_qty_override: float | None = None,
    spots: dict[str, float] | None = None,
) -> float:
    entry = float(suggestion.entry)  # type: ignore[arg-type]
    stop = float(suggestion.stop_loss)  # type: ignore[arg-type]
    product_id = getattr(suggestion, "product_id", None) or "ETH-USD"
    min_qty, max_qty = bot_config.qty_caps(product_id)
    resolved = spots or {product_id: float(spot)}
    eth_qty = (
        eth_qty_override
        if eth_qty_override is not None
        else _open_eth_qty(suggestion, cash)
    )
    if eth_qty_override is not None:
        if eth_qty <= 0 or entry <= 0 or cash <= 0:
            return cash
        max_affordable = cash / entry
        eth_qty = min(eth_qty, max_affordable, max_qty)
        if eth_qty < min_qty:
            return cash
    elif eth_qty <= 0:
        return cash

    side = "long" if suggestion.action in LONG_ACTIONS else "short"
    opened_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tranches = _merge_entry_tranches([], suggestion.entry_tranche)
    # One live plan per product: a fresh suggestion replaces whatever was still
    # waiting for its pullback, filled or not.
    conn.execute(
        "DELETE FROM paper_positions WHERE status = 'pending' AND product_id = ?",
        (product_id,),
    )
    if not _entry_is_fillable(side, entry, float(spot)):
        _record_pending_entry(
            conn, suggestion, eth_qty, cycle_id, product_id, side, opened_at
        )
        return cash

    notional = eth_qty * entry
    cash -= notional
    cursor = conn.execute(
        """
        INSERT INTO paper_positions (
            open_cycle_id, opened_at, side, action, eth_qty, qty, product_id, avg_entry,
            stop_loss, take_profits, risk_reward, suggested_size, status,
            order_block_ref, entry_tranches
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (
            cycle_id,
            opened_at,
            side,
            suggestion.action,
            eth_qty,
            eth_qty,
            product_id,
            entry,
            stop,
            json.dumps(suggestion.take_profits),
            suggestion.risk_reward,
            suggestion.size,
            suggestion.order_block_ref,
            json.dumps(tranches) if tranches else None,
        ),
    )
    pos_id = int(cursor.lastrowid)
    positions = _fetch_open_positions(conn)
    equity = _equity(cash, positions, resolved)
    _log_trade(
        conn,
        "open",
        cycle_id,
        side,
        eth_qty,
        entry,
        cash,
        equity,
        pos_id,
        None,
        product_id=product_id,
    )
    return cash


def update(
    suggestion: Suggestion,
    spot_price: float,
    cycle_id: str | None = None,
    spots: dict[str, float] | None = None,
) -> dict:
    """Apply latest suggestion to paper portfolio. Returns updated state dict."""
    init_db()
    _PENDING_OUTCOMES.clear()
    resolved = _resolve_spots(spot_price, spots)
    product_id = getattr(suggestion, "product_id", None) or "ETH-USD"
    trade_spot = _spot_for(product_id, resolved)
    if trade_spot <= 0:
        trade_spot = float(spot_price)
    try:
        with _connect() as conn:
            state = dict(conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone())
            cash = float(state["cash_usd"])

            # Fill first, then resolve barriers: a plan touched this window is
            # a position by the time its stop and targets are checked.
            cash = _fill_pending_entries(conn, cash, resolved, cycle_id)
            cash = _check_sl_tp_closes(conn, cash, resolved, cycle_id)

            if suggestion.action in TRADE_ACTIONS:
                cash = _apply_trade_with_netting(
                    conn, cash, suggestion, trade_spot, cycle_id, spots=resolved
                )
            else:
                _cancel_pending_entry(conn, product_id)

            conn.execute(
                """
                UPDATE paper_state
                SET cash_usd = ?, last_cycle_id = ?, last_spot = ?
                WHERE id = 1
                """,
                (cash, cycle_id, float(spot_price)),
            )
            conn.commit()
    finally:
        flush_pending_outcome_charts()

    return get_state()


def trim_trades_opened_before(
    cutoff_iso: str,
    *,
    new_epoch_started_at: str | None = None,
) -> dict:
    """Remove live-book paper trades that opened before ``cutoff_iso``.

    Used to start the v2 experiment at a later date (e.g. 2026-08-01) without
    archiving into v1. Closed-trade realized P&L is reversed out of cash;
    still-open pre-cutoff positions return their entry notional. Metrics then
    recompute from the remaining log.
    """
    init_db()
    cutoff = str(cutoff_iso)
    epoch_start = new_epoch_started_at or cutoff
    with _connect() as conn:
        state = dict(conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone())
        cash = float(state["cash_usd"])
        rows = [
            dict(row)
            for row in conn.execute("SELECT * FROM paper_trades ORDER BY id ASC")
        ]
        closed = _pair_closed_trades(rows)
        dropped_closed = [
            t for t in closed if str(t.get("opened_at") or "") < cutoff
        ]
        realized = sum(float(t.get("realized_pnl_usd") or 0) for t in dropped_closed)
        cash -= realized

        pos_ids: set[int] = set()
        for row in rows:
            if row.get("event") != "open":
                continue
            if str(row.get("ts") or "") >= cutoff:
                continue
            pid = row.get("position_id")
            if pid is not None:
                pos_ids.add(int(pid))

        restored_open = 0.0
        dropped_open = 0
        for pos in list(_fetch_open_positions(conn)):
            opened = str(pos.get("opened_at") or "")
            if opened >= cutoff:
                continue
            qty = _pos_qty(pos)
            entry = float(pos["avg_entry"])
            restored_open += qty * entry
            pos_ids.add(int(pos["id"]))
            dropped_open += 1

        cash += restored_open
        trade_deleted = 0
        if pos_ids:
            placeholders = ",".join("?" * len(pos_ids))
            cur = conn.execute(
                f"DELETE FROM paper_trades WHERE position_id IN ({placeholders})",
                tuple(pos_ids),
            )
            trade_deleted += int(cur.rowcount or 0)
            conn.execute(
                f"DELETE FROM paper_positions WHERE id IN ({placeholders})",
                tuple(pos_ids),
            )
        orphan = conn.execute(
            """
            DELETE FROM paper_trades
            WHERE ts < ? AND (position_id IS NULL OR position_id = 0)
            """,
            (cutoff,),
        )
        trade_deleted += int(orphan.rowcount or 0)

        conn.execute(
            """
            UPDATE paper_state
            SET cash_usd = ?, epoch_started_at = ?
            WHERE id = 1
            """,
            (cash, epoch_start),
        )
        conn.commit()

    return {
        "cutoff": cutoff,
        "dropped_closed": len(dropped_closed),
        "dropped_open": dropped_open,
        "trade_rows_deleted": trade_deleted,
        "realized_reversed_usd": round(realized, 2),
        "open_notional_restored_usd": round(restored_open, 2),
        "cash_usd": round(cash, 2),
        "epoch_started_at": epoch_start,
    }


def archive_epoch_and_reset(
    *,
    starting_usd: float | None = None,
    epoch_label: str | None = None,
    prior_epoch_label: str | None = None,
) -> dict:
    """Archive current paper trades/positions and start a fresh epoch.

    Returns a summary dict with counts and new starting balance.
    """
    init_db()
    starting = float(
        starting_usd if starting_usd is not None else config.PAPER_PORTFOLIO_VALUE
    )
    new_label = epoch_label or bot_config.PAPER_EPOCH_LABEL
    archived_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with _connect() as conn:
        state = dict(conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone())
        old_label = prior_epoch_label or state.get("epoch_label") or "legacy_1k"
        old_starting = float(state.get("starting_usd") or 0)

        trade_rows = conn.execute("SELECT * FROM paper_trades ORDER BY id ASC").fetchall()
        position_rows = conn.execute("SELECT * FROM paper_positions ORDER BY id ASC").fetchall()

        for row in trade_rows:
            data = dict(row)
            qty = data.get("qty")
            if qty is None:
                qty = data.get("eth_qty")
            conn.execute(
                """
                INSERT INTO paper_trades_archive (
                    source_id, ts, cycle_id, event, side, eth_qty, qty, product_id, price,
                    cash_usd, equity_usd, position_id, close_reason,
                    archived_at, epoch_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["id"],
                    data["ts"],
                    data.get("cycle_id"),
                    data["event"],
                    data.get("side"),
                    data.get("eth_qty"),
                    qty,
                    data.get("product_id") or "ETH-USD",
                    data.get("price"),
                    data.get("cash_usd"),
                    data.get("equity_usd"),
                    data.get("position_id"),
                    data.get("close_reason"),
                    archived_at,
                    old_label,
                ),
            )

        for row in position_rows:
            data = dict(row)
            qty = data.get("qty")
            if qty is None:
                qty = data.get("eth_qty")
            conn.execute(
                """
                INSERT INTO paper_positions_archive (
                    source_id, open_cycle_id, opened_at, side, action, eth_qty, qty,
                    product_id, avg_entry, stop_loss, take_profits, risk_reward,
                    suggested_size, status, archived_at, epoch_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["id"],
                    data["open_cycle_id"],
                    data["opened_at"],
                    data["side"],
                    data["action"],
                    data["eth_qty"],
                    qty,
                    data.get("product_id") or "ETH-USD",
                    data["avg_entry"],
                    data["stop_loss"],
                    data["take_profits"],
                    data.get("risk_reward"),
                    data.get("suggested_size"),
                    data["status"],
                    archived_at,
                    old_label,
                ),
            )

        if trade_rows or position_rows:
            conn.execute(
                """
                INSERT INTO paper_epochs (
                    label, starting_usd, ended_at,
                    archived_trade_rows, archived_position_rows
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    old_label,
                    old_starting,
                    archived_at,
                    len(trade_rows),
                    len(position_rows),
                ),
            )

        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM paper_positions")
        conn.execute("DELETE FROM paper_contributions")
        conn.execute(
            """
            UPDATE paper_state
            SET starting_usd = ?, cash_usd = ?, last_cycle_id = NULL, last_spot = NULL,
                epoch_started_at = ?, epoch_label = ?, total_contributed_usd = ?
            WHERE id = 1
            """,
            (starting, starting, archived_at, new_label, starting),
        )
        conn.execute(
            """
            INSERT INTO paper_contributions (telegram_id, amount_usd, created_at, username)
            VALUES (?, ?, ?, ?)
            """,
            (
                int(bot_config.HOUSE_CONTRIBUTION_TELEGRAM_ID),
                starting,
                archived_at,
                "house",
            ),
        )
        conn.commit()

    return {
        "archived_at": archived_at,
        "prior_epoch_label": old_label,
        "prior_starting_usd": old_starting,
        "archived_trade_rows": len(trade_rows),
        "archived_position_rows": len(position_rows),
        "new_epoch_label": new_label,
        "new_starting_usd": starting,
    }


class OpenPositionConflictError(ValueError):
    """Raised when restore_open_position would overwrite an existing open position."""


def restore_open_position(
    *,
    action: str,
    entry: float,
    eth_qty: float,
    stop_loss: float,
    take_profits: list[float],
    risk_reward: float,
    suggested_size: float,
    opened_at: str,
    open_cycle_id: str,
    spot_price: float,
    force: bool = False,
    product_id: str = "ETH-USD",
) -> dict:
    """Manually set an open paper position (e.g. backfill after a missed broadcast)."""
    init_db()
    _PENDING_OUTCOMES.clear()
    product_id = product_id or "ETH-USD"
    min_qty, max_qty = bot_config.qty_caps(product_id)
    spots = _resolve_spots(spot_price, {product_id: float(spot_price)})
    try:
        with _connect() as conn:
            positions = _fetch_open_positions(conn)
            for pos in positions:
                if str(pos.get("open_cycle_id")) == open_cycle_id:
                    return get_state()

            if positions and not force:
                existing = positions[0]
                raise OpenPositionConflictError(
                    f"Paper already has {existing.get('action')} open "
                    f"(cycle {existing.get('open_cycle_id')}); refusing to add "
                    f"{action} (cycle {open_cycle_id}). Pass force=True to close first."
                )

            state = dict(conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone())
            cash = float(state["cash_usd"])
            side = "long" if action in LONG_ACTIONS else "short"
            eth_qty = max(min_qty, min(max_qty, eth_qty))
            notional = eth_qty * entry

            if force and positions:
                for pos in list(_fetch_open_positions(conn)):
                    pos_spot = _spot_for(_pos_product(pos), spots)
                    if pos_spot <= 0:
                        pos_spot = float(spot_price)
                    cash = _close_position_at_market(
                        conn,
                        cash,
                        pos,
                        pos_spot,
                        open_cycle_id,
                        "restore_force",
                        spots=spots,
                    )
                positions = []

            if len(positions) >= bot_config.MAX_OPEN_TRADES:
                oldest = positions[0]
                oldest_spot = _spot_for(_pos_product(oldest), spots)
                if oldest_spot <= 0:
                    oldest_spot = float(spot_price)
                cash = _close_position_at_market(
                    conn,
                    cash,
                    oldest,
                    oldest_spot,
                    open_cycle_id,
                    "fifo_max_positions",
                    spots=spots,
                )

            if cash < notional:
                raise ValueError(
                    f"Notional ${notional:,.2f} exceeds available cash ${cash:,.2f}"
                )

            cash -= notional
            cursor = conn.execute(
                """
                INSERT INTO paper_positions (
                    open_cycle_id, opened_at, side, action, eth_qty, qty, product_id,
                    avg_entry, stop_loss, take_profits, risk_reward, suggested_size, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    open_cycle_id,
                    opened_at,
                    side,
                    action,
                    eth_qty,
                    eth_qty,
                    product_id,
                    entry,
                    stop_loss,
                    json.dumps(take_profits),
                    risk_reward,
                    suggested_size,
                ),
            )
            pos_id = int(cursor.lastrowid)
            all_open = _fetch_open_positions(conn)
            equity = _equity(cash, all_open, spots)
            _log_trade(
                conn,
                "open",
                open_cycle_id,
                side,
                eth_qty,
                entry,
                cash,
                equity,
                pos_id,
                None,
                product_id=product_id,
            )
            conn.execute(
                "UPDATE paper_state SET cash_usd = ?, last_cycle_id = ?, last_spot = ? WHERE id = 1",
                (cash, open_cycle_id, spot_price),
            )
            conn.commit()
    finally:
        flush_pending_outcome_charts()

    return get_state()


def format_pnl_footer(
    spot_price: float | None = None,
    spots: dict[str, float] | None = None,
) -> str:
    """One-line paper PnL summary for Telegram messages."""
    state = get_state()
    resolved = _resolve_spots(spot_price, spots)
    if not resolved:
        last = state.get("last_spot")
        if last is not None and float(last) > 0:
            resolved = {"ETH-USD": float(last)}

    starting = float(state["starting_usd"])
    cash = float(state["cash_usd"])
    positions = get_open_positions(spot_price, spots=resolved)

    for pos in positions:
        pid = _pos_product(pos)
        if _spot_for(pid, resolved) <= 0:
            resolved[pid] = float(pos["spot"])

    equity = _equity(cash, positions, resolved) if resolved else cash
    pnl = equity - starting
    pnl_pct = (pnl / starting * 100) if starting else 0.0

    if not positions:
        pos = "Flat"
    elif len(positions) == 1:
        p = positions[0]
        side = str(p["side"])
        asset = bot_config.product_label(_pos_product(p))
        if side == "long":
            pos = f"Long {_pos_qty(p):.4f} {asset} @ {float(p['avg_entry']):,.2f}"
        else:
            pos = f"Short {_pos_qty(p):.4f} {asset} @ {float(p['avg_entry']):,.2f}"
    else:
        pos = f"{len(positions)} open positions"

    display_spot = _spot_for("ETH-USD", resolved)
    if display_spot <= 0 and positions:
        display_spot = float(positions[0]["spot"])

    sign = "+" if pnl >= 0 else ""
    return (
        f"Paper PnL (${starting:,.0f} start): ${equity:,.2f} ({sign}{pnl_pct:.2f}%) "
        f"| {pos} | Spot: ${display_spot:,.2f}"
    )


def fund_user(telegram_id: int, username: str | None = None) -> dict:
    """Deprecated: opens a $1k personal demo account (no longer funds house book)."""
    import user_books

    init_db()
    result = user_books.open_paper_account(
        telegram_id,
        float(bot_config.PAPER_ACCOUNT_DEFAULT_USD),
        username=username,
    )
    if result.get("ok"):
        return {
            "ok": True,
            "amount": result["amount_usd"],
            "amount_usd": result["amount_usd"],
            "share_pct": 100.0,
            "cash_usd": result["cash_usd"],
            "total_contributed_usd": result["amount_usd"],
        }
    if result.get("reason") == "already_opened":
        return {
            "ok": False,
            "reason": "already_funded",
            "amount": result.get("amount_usd"),
            "amount_usd": result.get("amount_usd"),
            "share_pct": 100.0,
            "username": username,
        }
    return {"ok": False, "reason": result.get("reason") or "failed"}


def get_contribution(telegram_id: int) -> dict | None:
    """Legacy contribution row, or a synthetic row from personal account."""
    import user_books

    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM paper_contributions WHERE telegram_id = ?",
            (int(telegram_id),),
        ).fetchone()
    if row:
        return dict(row)
    account = user_books.get_account(telegram_id)
    if account is None:
        return None
    return {
        "telegram_id": int(telegram_id),
        "amount_usd": float(account["starting_usd"]),
        "created_at": account.get("opened_at"),
        "username": account.get("username"),
    }


def list_contributions() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_contributions ORDER BY created_at ASC, telegram_id ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def total_contributed() -> float:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT total_contributed_usd FROM paper_state WHERE id = 1"
        ).fetchone()
    if row and row["total_contributed_usd"] is not None:
        return float(row["total_contributed_usd"])
    return sum(float(c["amount_usd"]) for c in list_contributions())


def get_user_metrics(
    telegram_id: int,
    spots: dict[str, float] | None = None,
) -> dict:
    """Personal demo book equity / PnL (not a share of the house book)."""
    import user_books

    init_db()
    return user_books.get_user_metrics(telegram_id, spots=spots)
