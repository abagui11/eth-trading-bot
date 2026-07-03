"""Paper portfolio tracker — 1% risk sizing with min/max ETH bounds per Trading Guide."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import bot_config
import config
from models import Suggestion

LONG_ACTIONS = {"spot_buy", "deriv_buy"}
SHORT_ACTIONS = {"spot_sell", "deriv_sell"}
TRADE_ACTIONS = LONG_ACTIONS | SHORT_ACTIONS

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
            open_cycle_id, opened_at, side, action, eth_qty, avg_entry,
            stop_loss, take_profits, risk_reward, suggested_size, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            data["open_cycle_id"],
            data.get("opened_at")
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            side,
            data.get("action") or side,
            eth_qty,
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
        _ensure_legacy_columns(conn)
        _ensure_trade_columns(conn)
        _ensure_state_epoch_columns(conn)
        row = conn.execute("SELECT id FROM paper_state WHERE id = 1").fetchone()
        if row is None:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """
                INSERT INTO paper_state (
                    id, starting_usd, cash_usd, epoch_started_at, epoch_label
                )
                VALUES (1, ?, ?, ?, ?)
                """,
                (
                    config.PAPER_PORTFOLIO_VALUE,
                    config.PAPER_PORTFOLIO_VALUE,
                    now,
                    bot_config.PAPER_EPOCH_LABEL,
                ),
            )
        _migrate_legacy_position(conn)
        conn.commit()


def _equity(
    cash: float,
    positions: list[dict],
    spot: float,
) -> float:
    total = cash
    for pos in positions:
        side = str(pos["side"])
        eth_qty = float(pos["eth_qty"])
        avg_entry = float(pos["avg_entry"])
        if side == "long":
            total += eth_qty * spot
        elif side == "short":
            total += eth_qty * (2 * avg_entry - spot)
    return total


def _unrealized_pnl(side: str, eth_qty: float, avg_entry: float, spot: float) -> float:
    if eth_qty <= 0:
        return 0.0
    if side == "long":
        return eth_qty * (spot - avg_entry)
    return eth_qty * (avg_entry - spot)


def _position_usd(entry: float, stop_loss: float) -> float:
    risk_usd = config.PAPER_PORTFOLIO_VALUE * 0.01
    sl_pct = abs(entry - stop_loss) / entry
    if sl_pct <= 0:
        return 0.0
    return risk_usd / sl_pct


def _compute_eth_qty(entry: float, stop_loss: float, cash: float) -> float:
    """1% risk sizing, capped by cash and bot_config MIN/MAX ETH bounds."""
    if entry <= 0 or cash <= 0:
        return 0.0
    notional = min(_position_usd(entry, stop_loss), cash)
    if notional <= 0:
        return 0.0
    eth_qty = notional / entry
    eth_qty = min(eth_qty, bot_config.MAX_ETH_QTY)
    if eth_qty < bot_config.MIN_ETH_QTY:
        min_notional = bot_config.MIN_ETH_QTY * entry
        if min_notional > cash:
            return 0.0
        eth_qty = bot_config.MIN_ETH_QTY
    return eth_qty


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


def get_open_positions(spot_price: float | None = None) -> list[dict]:
    """Return all open paper positions enriched with spot and unrealized P&L."""
    init_db()
    spot = spot_price
    if spot is None:
        state = get_state()
        spot = state.get("last_spot")
    if spot is None or float(spot) <= 0:
        try:
            import research

            spot = research.get_spot_price()
        except Exception:
            spot = 0.0
    spot_f = float(spot)

    with _connect() as conn:
        positions = _fetch_open_positions(conn)

    starting = float(get_state()["starting_usd"])
    enriched: list[dict] = []
    for pos in positions:
        side = str(pos["side"])
        eth_qty = float(pos["eth_qty"])
        avg_entry = float(pos["avg_entry"])
        unrealized = _unrealized_pnl(side, eth_qty, avg_entry, spot_f)
        enriched.append(
            {
                **pos,
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


def get_open_position(spot_price: float | None = None) -> dict | None:
    """Return the oldest open position, or None if flat."""
    positions = get_open_positions(spot_price)
    if not positions:
        return None
    pos = positions[0]
    starting = float(pos["starting_usd"])
    cash = float(get_state()["cash_usd"])
    spot_f = float(pos["spot"])
    all_open = get_open_positions(spot_f)
    equity = _equity(cash, all_open, spot_f)
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
            lines.append(f"TP{idx} at ${tp_f:,.2f} — scale out on downside ({status}).")
        else:
            status = "hit" if spot >= tp_f else "pending"
            lines.append(f"TP{idx} at ${tp_f:,.2f} — scale out on upside ({status}).")

    if not lines:
        return "No SL/TP levels recorded for this position."
    return "\n".join(lines)


def _format_single_position(position: dict, index: int | None = None) -> str:
    side = str(position["side"])
    action = str(position.get("action") or side).upper()
    eth_qty = float(position["eth_qty"])
    entry = float(position["avg_entry"])
    spot = float(position["spot"])
    unrealized = float(position["unrealized_pnl_usd"])
    sign = "+" if unrealized >= 0 else ""

    label = "Long ETH" if side == "long" else "Short ETH"
    prefix = f"Position {index}: " if index is not None else "Open position: "
    lines = [
        f"{prefix}{action} ({label})",
        f"Entered: {position.get('opened_at') or 'unknown'} (cycle {position.get('open_cycle_id') or 'n/a'})",
        f"Size: {eth_qty:.4f} ETH",
    ]
    if position.get("suggested_size") is not None:
        lines[-1] += f" (suggested {float(position['suggested_size']):.2f})"
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
    """Pair open/close ledger rows into closed trade summaries (oldest-first input)."""
    pending_opens: dict[int | None, list[dict]] = {}
    closed: list[dict] = []

    for row in rows:
        event = str(row.get("event") or "")
        pos_id = row.get("position_id")
        if event == "open":
            pending_opens.setdefault(pos_id, []).append(row)
            continue
        if event != "close":
            continue

        side = str(row.get("side") or "")
        opened: dict | None = None
        if pos_id is not None and pos_id in pending_opens and pending_opens[pos_id]:
            opened = pending_opens[pos_id].pop()
        if opened is None:
            for opens in pending_opens.values():
                for i in range(len(opens) - 1, -1, -1):
                    if str(opens[i].get("side") or "") == side:
                        opened = opens.pop(i)
                        break
                if opened:
                    break
        if opened is None:
            continue

        entry = float(opened["price"])
        exit_price = float(row["price"])
        qty = float(opened["eth_qty"])
        if side == "long":
            realized_pnl = qty * (exit_price - entry)
        else:
            realized_pnl = qty * (entry - exit_price)
        notional = qty * entry
        closed.append(
            {
                "side": side,
                "open_cycle_id": opened.get("cycle_id"),
                "close_cycle_id": row.get("cycle_id"),
                "eth_qty": qty,
                "entry": entry,
                "exit": exit_price,
                "opened_at": opened.get("ts"),
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
        lines.append(
            f"{idx}. {action.upper()} {float(trade['eth_qty']):.4f} ETH "
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
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO paper_trades (
            ts, cycle_id, event, side, eth_qty, price, cash_usd, equity_usd,
            position_id, close_reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, cycle_id, event, side, eth_qty, price, cash, equity, position_id, close_reason),
    )


def _close_position_at_market(
    conn: sqlite3.Connection,
    cash: float,
    position: dict,
    spot: float,
    cycle_id: str | None,
    reason: str,
) -> float:
    side = str(position["side"])
    eth_qty = float(position["eth_qty"])
    avg_entry = float(position["avg_entry"])
    pos_id = int(position["id"])

    if side == "long":
        cash += eth_qty * spot
    elif side == "short":
        cash += eth_qty * (2 * avg_entry - spot)

    open_positions = [p for p in _fetch_open_positions(conn) if int(p["id"]) != pos_id]
    equity = _equity(cash, open_positions, spot)
    conn.execute(
        "UPDATE paper_positions SET status = 'closed' WHERE id = ?",
        (pos_id,),
    )
    _log_trade(
        conn, "close", cycle_id, side, eth_qty, spot, cash, equity, pos_id, reason
    )
    return cash


def _sl_hit(side: str, spot: float, stop_loss: float) -> bool:
    if side == "long":
        return spot <= stop_loss
    return spot >= stop_loss


def _tp_hit(side: str, spot: float, take_profits: list[float]) -> bool:
    if not take_profits:
        return False
    if side == "long":
        return spot >= min(take_profits)
    return spot <= max(take_profits)


def _check_sl_tp_closes(
    conn: sqlite3.Connection,
    cash: float,
    spot: float,
    cycle_id: str | None,
) -> float:
    for position in list(_fetch_open_positions(conn)):
        side = str(position["side"])
        sl = float(position["stop_loss"])
        tps = position.get("take_profits") or []
        if _sl_hit(side, spot, sl):
            cash = _close_position_at_market(conn, cash, position, sl, cycle_id, "stop_loss")
        elif _tp_hit(side, spot, tps):
            tp_price = min(tps) if side == "long" else max(tps)
            cash = _close_position_at_market(conn, cash, position, tp_price, cycle_id, "take_profit")
    return cash


def _open_position(
    conn: sqlite3.Connection,
    cash: float,
    suggestion: Suggestion,
    spot: float,
    cycle_id: str | None,
) -> float:
    entry = float(suggestion.entry)  # type: ignore[arg-type]
    stop = float(suggestion.stop_loss)  # type: ignore[arg-type]
    eth_qty = _compute_eth_qty(entry, stop, cash)
    if eth_qty <= 0:
        return cash

    notional = eth_qty * entry
    cash -= notional
    side = "long" if suggestion.action in LONG_ACTIONS else "short"
    opened_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = conn.execute(
        """
        INSERT INTO paper_positions (
            open_cycle_id, opened_at, side, action, eth_qty, avg_entry,
            stop_loss, take_profits, risk_reward, suggested_size, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            cycle_id,
            opened_at,
            side,
            suggestion.action,
            eth_qty,
            entry,
            stop,
            json.dumps(suggestion.take_profits),
            suggestion.risk_reward,
            suggestion.size,
        ),
    )
    pos_id = int(cursor.lastrowid)
    positions = _fetch_open_positions(conn)
    equity = _equity(cash, positions, spot)
    _log_trade(conn, "open", cycle_id, side, eth_qty, entry, cash, equity, pos_id, None)
    return cash


def update(suggestion: Suggestion, spot_price: float, cycle_id: str | None = None) -> dict:
    """Apply latest suggestion to paper portfolio. Returns updated state dict."""
    init_db()
    with _connect() as conn:
        state = dict(conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone())
        cash = float(state["cash_usd"])

        cash = _check_sl_tp_closes(conn, cash, spot_price, cycle_id)

        if suggestion.action in TRADE_ACTIONS:
            open_positions = _fetch_open_positions(conn)
            while len(open_positions) >= bot_config.MAX_OPEN_TRADES:
                oldest = open_positions[0]
                cash = _close_position_at_market(
                    conn, cash, oldest, spot_price, cycle_id, "fifo_max_positions"
                )
                open_positions = _fetch_open_positions(conn)

            cash = _open_position(conn, cash, suggestion, spot_price, cycle_id)

        conn.execute(
            """
            UPDATE paper_state
            SET cash_usd = ?, last_cycle_id = ?, last_spot = ?
            WHERE id = 1
            """,
            (cash, cycle_id, spot_price),
        )
        conn.commit()

    return get_state()


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
            conn.execute(
                """
                INSERT INTO paper_trades_archive (
                    source_id, ts, cycle_id, event, side, eth_qty, price,
                    cash_usd, equity_usd, position_id, close_reason,
                    archived_at, epoch_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["id"],
                    data["ts"],
                    data.get("cycle_id"),
                    data["event"],
                    data.get("side"),
                    data.get("eth_qty"),
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
            conn.execute(
                """
                INSERT INTO paper_positions_archive (
                    source_id, open_cycle_id, opened_at, side, action, eth_qty,
                    avg_entry, stop_loss, take_profits, risk_reward, suggested_size,
                    status, archived_at, epoch_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["id"],
                    data["open_cycle_id"],
                    data["opened_at"],
                    data["side"],
                    data["action"],
                    data["eth_qty"],
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
        conn.execute(
            """
            UPDATE paper_state
            SET starting_usd = ?, cash_usd = ?, last_cycle_id = NULL, last_spot = NULL,
                epoch_started_at = ?, epoch_label = ?
            WHERE id = 1
            """,
            (starting, starting, archived_at, new_label),
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
) -> dict:
    """Manually set an open paper position (e.g. backfill after a missed broadcast)."""
    init_db()
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
        eth_qty = max(bot_config.MIN_ETH_QTY, min(bot_config.MAX_ETH_QTY, eth_qty))
        notional = eth_qty * entry

        if force and positions:
            for pos in list(_fetch_open_positions(conn)):
                cash = _close_position_at_market(
                    conn, cash, pos, spot_price, open_cycle_id, "restore_force"
                )
            positions = []

        if len(positions) >= bot_config.MAX_OPEN_TRADES:
            oldest = positions[0]
            cash = _close_position_at_market(
                conn, cash, oldest, spot_price, open_cycle_id, "fifo_max_positions"
            )

        if cash < notional:
            raise ValueError(
                f"Notional ${notional:,.2f} exceeds available cash ${cash:,.2f}"
            )

        cash -= notional
        cursor = conn.execute(
            """
            INSERT INTO paper_positions (
                open_cycle_id, opened_at, side, action, eth_qty, avg_entry,
                stop_loss, take_profits, risk_reward, suggested_size, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                open_cycle_id,
                opened_at,
                side,
                action,
                eth_qty,
                entry,
                stop_loss,
                json.dumps(take_profits),
                risk_reward,
                suggested_size,
            ),
        )
        pos_id = int(cursor.lastrowid)
        all_open = _fetch_open_positions(conn)
        equity = _equity(cash, all_open, spot_price)
        _log_trade(
            conn, "open", open_cycle_id, side, eth_qty, entry, cash, equity, pos_id, None
        )
        conn.execute(
            "UPDATE paper_state SET cash_usd = ?, last_cycle_id = ?, last_spot = ? WHERE id = 1",
            (cash, open_cycle_id, spot_price),
        )
        conn.commit()

    return get_state()


def format_pnl_footer(spot_price: float | None = None) -> str:
    """One-line paper PnL summary for Telegram messages."""
    state = get_state()
    spot = spot_price if spot_price is not None else state.get("last_spot")
    if spot is None or float(spot) <= 0:
        try:
            import research

            spot = research.get_spot_price()
        except Exception:
            spot = 0.0

    starting = float(state["starting_usd"])
    cash = float(state["cash_usd"])
    positions = get_open_positions(float(spot))

    equity = _equity(cash, positions, float(spot))
    pnl = equity - starting
    pnl_pct = (pnl / starting * 100) if starting else 0.0

    if not positions:
        pos = "Flat"
    elif len(positions) == 1:
        p = positions[0]
        side = str(p["side"])
        if side == "long":
            pos = f"Long {float(p['eth_qty']):.4f} ETH @ {float(p['avg_entry']):,.2f}"
        else:
            pos = f"Short {float(p['eth_qty']):.4f} ETH @ {float(p['avg_entry']):,.2f}"
    else:
        pos = f"{len(positions)} open positions"

    sign = "+" if pnl >= 0 else ""
    return (
        f"Paper PnL (${starting:,.0f} start): ${equity:,.2f} ({sign}{pnl_pct:.2f}%) "
        f"| {pos} | Spot: ${float(spot):,.2f}"
    )
