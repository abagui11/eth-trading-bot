"""Paper portfolio tracker — $1000 start, 1% risk sizing per Trading Guide."""

from __future__ import annotations

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


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_STATE_SCHEMA)
        conn.execute(_TRADES_SCHEMA)
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
        # Short: profit when spot falls below entry.
        return cash + eth_qty * (2 * avg_entry - spot)
    return cash


def _position_usd(entry: float, stop_loss: float) -> float:
    risk_usd = config.PAPER_PORTFOLIO_VALUE * 0.01
    sl_pct = abs(entry - stop_loss) / entry
    if sl_pct <= 0:
        return 0.0
    return risk_usd / sl_pct


def get_state() -> dict:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone()
    return dict(row)


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

        if suggestion.action in TRADE_ACTIONS:
            cash, side, eth_qty, avg_entry = _close_position(
                conn, cash, side, eth_qty, avg_entry, spot_price, cycle_id
            )

            entry = float(suggestion.entry)  # type: ignore[arg-type]
            stop = float(suggestion.stop_loss)  # type: ignore[arg-type]
            notional = _position_usd(entry, stop)
            notional = min(notional, cash)
            if notional > 0:
                eth_qty = notional / entry
                cash -= notional
                side = "long" if suggestion.action in LONG_ACTIONS else "short"
                avg_entry = entry
                equity = _equity(cash, side, eth_qty, avg_entry, spot_price)
                _log_trade(conn, "open", cycle_id, side, eth_qty, entry, cash, equity)

        equity = _equity(cash, side, eth_qty, avg_entry, spot_price)

        conn.execute(
            """
            UPDATE paper_state
            SET cash_usd = ?, side = ?, eth_qty = ?, avg_entry = ?,
                last_cycle_id = ?, last_spot = ?
            WHERE id = 1
            """,
            (cash, side, eth_qty, avg_entry, cycle_id, spot_price),
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
