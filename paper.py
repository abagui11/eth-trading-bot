"""Paper portfolio tracker — $1000 start, 1% risk sizing per Trading Guide."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

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
    side TEXT NOT NULL DEFAULT 'flat',
    eth_qty REAL NOT NULL DEFAULT 0,
    avg_entry REAL,
    last_cycle_id TEXT,
    last_spot REAL
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
    equity_usd REAL
);
"""

_POSITION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("action", "TEXT"),
    ("stop_loss", "REAL"),
    ("take_profits", "TEXT"),
    ("risk_reward", "REAL"),
    ("suggested_size", "REAL"),
    ("opened_at", "TEXT"),
    ("open_cycle_id", "TEXT"),
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_position_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_state)").fetchall()}
    for name, col_type in _POSITION_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE paper_state ADD COLUMN {name} {col_type}")


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_STATE_SCHEMA)
        conn.execute(_TRADES_SCHEMA)
        _ensure_position_columns(conn)
        row = conn.execute("SELECT id FROM paper_state WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO paper_state (id, starting_usd, cash_usd, side, eth_qty)
                VALUES (1, ?, ?, 'flat', 0)
                """,
                (config.PAPER_PORTFOLIO_VALUE, config.PAPER_PORTFOLIO_VALUE),
            )
        conn.commit()


def _equity(cash: float, side: str, eth_qty: float, avg_entry: float | None, spot: float) -> float:
    if side == "long" and eth_qty > 0:
        return cash + eth_qty * spot
    if side == "short" and eth_qty > 0 and avg_entry is not None:
        return cash + eth_qty * (2 * avg_entry - spot)
    return cash


def _unrealized_pnl(side: str, eth_qty: float, avg_entry: float | None, spot: float) -> float:
    if side == "flat" or eth_qty <= 0 or avg_entry is None:
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


def _parse_take_profits(raw: str | None) -> list[float]:
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [float(tp) for tp in values]


def get_state() -> dict:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone()
    state = dict(row)
    state["take_profits"] = _parse_take_profits(state.get("take_profits"))
    return state


def is_open(state: dict | None = None) -> bool:
    state = state or get_state()
    return str(state.get("side")) != "flat" and float(state.get("eth_qty") or 0) > 0


def get_open_position(spot_price: float | None = None) -> dict | None:
    """Return enriched open position dict, or None if flat."""
    state = get_state()
    if not is_open(state):
        return None

    spot = spot_price if spot_price is not None else state.get("last_spot")
    if spot is None or float(spot) <= 0:
        try:
            import research

            spot = research.get_spot_price()
        except Exception:
            spot = 0.0

    spot_f = float(spot)
    side = str(state["side"])
    eth_qty = float(state["eth_qty"])
    avg_entry = float(state["avg_entry"])
    starting = float(state["starting_usd"])
    cash = float(state["cash_usd"])
    equity = _equity(cash, side, eth_qty, avg_entry, spot_f)
    unrealized = _unrealized_pnl(side, eth_qty, avg_entry, spot_f)

    return {
        "side": side,
        "action": state.get("action"),
        "eth_qty": eth_qty,
        "suggested_size": state.get("suggested_size"),
        "avg_entry": avg_entry,
        "stop_loss": state.get("stop_loss"),
        "take_profits": state.get("take_profits") or [],
        "risk_reward": state.get("risk_reward"),
        "opened_at": state.get("opened_at"),
        "open_cycle_id": state.get("open_cycle_id"),
        "spot": spot_f,
        "unrealized_pnl_usd": unrealized,
        "equity_usd": equity,
        "starting_usd": starting,
        "portfolio_pnl_usd": equity - starting,
        "portfolio_pnl_pct": ((equity - starting) / starting * 100) if starting else 0.0,
    }


def _format_exit_plan(position: dict) -> str:
    side = str(position["side"])
    entry = float(position["avg_entry"])
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


def format_position_detail(spot_price: float | None = None) -> str | None:
    """Multi-line breakdown of the open paper position, or None if flat."""
    position = get_open_position(spot_price)
    if position is None:
        return None

    side = str(position["side"])
    action = str(position.get("action") or side).upper()
    eth_qty = float(position["eth_qty"])
    entry = float(position["avg_entry"])
    spot = float(position["spot"])
    unrealized = float(position["unrealized_pnl_usd"])
    sign = "+" if unrealized >= 0 else ""

    label = "Long ETH" if side == "long" else "Short ETH"
    lines = [
        f"Open position: {action} ({label})",
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

    lines.append("")
    lines.append("Exit plan:")
    lines.append(_format_exit_plan(position))
    return "\n".join(lines)


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

    pending_opens: list[dict] = []
    closed: list[dict] = []

    for row in rows:
        event = str(row.get("event") or "")
        if event == "open":
            pending_opens.append(row)
            continue
        if event != "close":
            continue

        side = str(row.get("side") or "")
        match_idx: int | None = None
        for i in range(len(pending_opens) - 1, -1, -1):
            if str(pending_opens[i].get("side") or "") == side:
                match_idx = i
                break
        if match_idx is None:
            continue

        opened = pending_opens.pop(match_idx)
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
                "realized_pnl_usd": realized_pnl,
                "realized_pnl_pct": (realized_pnl / notional * 100) if notional else 0.0,
            }
        )

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
        lines.append(
            f"{idx}. {action.upper()} {float(trade['eth_qty']):.4f} ETH "
            f"@ ${float(trade['entry']):,.2f} -> ${float(trade['exit']):,.2f} "
            f"| realized {pnl_str} "
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
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO paper_trades (ts, cycle_id, event, side, eth_qty, price, cash_usd, equity_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, cycle_id, event, side, eth_qty, price, cash, equity),
    )


def _close_position(
    conn: sqlite3.Connection,
    cash: float,
    side: str,
    eth_qty: float,
    avg_entry: float | None,
    spot: float,
    cycle_id: str | None,
) -> tuple[float, str, float, float | None]:
    if side == "flat" or eth_qty <= 0:
        return cash, "flat", 0.0, None

    if side == "long":
        cash += eth_qty * spot
    elif side == "short" and avg_entry is not None:
        cash += eth_qty * (2 * avg_entry - spot)

    equity = cash
    _log_trade(conn, "close", cycle_id, side, eth_qty, spot, cash, equity)
    return cash, "flat", 0.0, None


def update(suggestion: Suggestion, spot_price: float, cycle_id: str | None = None) -> dict:
    """Apply latest suggestion to paper portfolio. Returns updated state dict."""
    init_db()
    with _connect() as conn:
        state = dict(conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone())
        cash = float(state["cash_usd"])
        side = str(state["side"])
        eth_qty = float(state["eth_qty"])
        avg_entry = state["avg_entry"]

        action: str | None = state.get("action")
        stop_loss: float | None = state.get("stop_loss")
        take_profits_json: str | None = state.get("take_profits")
        risk_reward: float | None = state.get("risk_reward")
        suggested_size: float | None = state.get("suggested_size")
        opened_at: str | None = state.get("opened_at")
        open_cycle_id: str | None = state.get("open_cycle_id")

        if suggestion.action in TRADE_ACTIONS:
            cash, side, eth_qty, avg_entry = _close_position(
                conn, cash, side, eth_qty, avg_entry, spot_price, cycle_id
            )
            action = None
            stop_loss = None
            take_profits_json = None
            risk_reward = None
            suggested_size = None
            opened_at = None
            open_cycle_id = None

            entry = float(suggestion.entry)  # type: ignore[arg-type]
            stop = float(suggestion.stop_loss)  # type: ignore[arg-type]
            notional = _position_usd(entry, stop)
            notional = min(notional, cash)
            if notional > 0:
                eth_qty = notional / entry
                cash -= notional
                side = "long" if suggestion.action in LONG_ACTIONS else "short"
                avg_entry = entry
                action = suggestion.action
                stop_loss = stop
                take_profits_json = json.dumps(suggestion.take_profits)
                risk_reward = suggestion.risk_reward
                suggested_size = suggestion.size
                opened_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                open_cycle_id = cycle_id
                equity = _equity(cash, side, eth_qty, avg_entry, spot_price)
                _log_trade(conn, "open", cycle_id, side, eth_qty, entry, cash, equity)

        conn.execute(
            """
            UPDATE paper_state
            SET cash_usd = ?, side = ?, eth_qty = ?, avg_entry = ?,
                last_cycle_id = ?, last_spot = ?,
                action = ?, stop_loss = ?, take_profits = ?,
                risk_reward = ?, suggested_size = ?, opened_at = ?, open_cycle_id = ?
            WHERE id = 1
            """,
            (
                cash,
                side,
                eth_qty,
                avg_entry,
                cycle_id,
                spot_price,
                action,
                stop_loss,
                take_profits_json,
                risk_reward,
                suggested_size,
                opened_at,
                open_cycle_id,
            ),
        )
        conn.commit()

    return get_state()


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
    """Manually set an open paper position (e.g. backfill after a missed broadcast).

    Refuses to overwrite a different open position unless force=True (closes first).
    Re-running with the same open_cycle_id is a no-op when already open.
    """
    init_db()
    state = get_state()
    if is_open(state):
        existing_cycle = str(state.get("open_cycle_id") or "")
        if existing_cycle == open_cycle_id:
            return state
        if not force:
            raise OpenPositionConflictError(
                f"Paper already has {state.get('action')} open "
                f"(cycle {existing_cycle}); refusing to overwrite with "
                f"{action} (cycle {open_cycle_id}). Pass force=True to close first."
            )

    side = "long" if action in LONG_ACTIONS else "short"
    notional = eth_qty * entry
    starting = config.PAPER_PORTFOLIO_VALUE
    cash = starting - notional
    if cash < 0:
        raise ValueError(f"Notional ${notional:,.2f} exceeds paper portfolio ${starting:,.2f}")

    with _connect() as conn:
        if is_open(state):
            cash = float(state["cash_usd"])
            cash, _, _, _ = _close_position(
                conn,
                cash,
                str(state["side"]),
                float(state["eth_qty"]),
                state["avg_entry"],
                spot_price,
                open_cycle_id,
            )
            notional = eth_qty * entry
            cash -= notional
            if cash < 0:
                raise ValueError(
                    f"Notional ${notional:,.2f} exceeds available cash ${cash + notional:,.2f}"
                )

        conn.execute(
            """
            UPDATE paper_state
            SET starting_usd = ?, cash_usd = ?, side = ?, eth_qty = ?, avg_entry = ?,
                last_cycle_id = ?, last_spot = ?,
                action = ?, stop_loss = ?, take_profits = ?,
                risk_reward = ?, suggested_size = ?, opened_at = ?, open_cycle_id = ?
            WHERE id = 1
            """,
            (
                starting,
                cash,
                side,
                eth_qty,
                entry,
                open_cycle_id,
                spot_price,
                action,
                stop_loss,
                json.dumps(take_profits),
                risk_reward,
                suggested_size,
                opened_at,
                open_cycle_id,
            ),
        )
        equity = _equity(cash, side, eth_qty, entry, spot_price)
        _log_trade(conn, "open", open_cycle_id, side, eth_qty, entry, cash, equity)
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
    side = str(state["side"])
    eth_qty = float(state["eth_qty"])
    avg_entry = state["avg_entry"]

    equity = _equity(cash, side, eth_qty, avg_entry, float(spot))
    pnl = equity - starting
    pnl_pct = (pnl / starting * 100) if starting else 0.0

    if side == "flat" or eth_qty <= 0:
        pos = "Flat"
    elif side == "long":
        pos = f"Long {eth_qty:.4f} ETH @ {float(avg_entry):,.2f}"
    else:
        pos = f"Short {eth_qty:.4f} ETH @ {float(avg_entry):,.2f}"

    sign = "+" if pnl >= 0 else ""
    return (
        f"Paper PnL (${starting:,.0f} start): ${equity:,.2f} ({sign}{pnl_pct:.2f}%) "
        f"| {pos} | Spot: ${float(spot):,.2f}"
    )
