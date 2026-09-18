"""The subscribable strategies: keys, labels, copy, and live risk profiles.

One place for everything /subscribe needs to know about a strategy so the
bot, the deep links from the website (t.me/...?start=subscribe_<key>), and
the per-strategy idea routing all agree on the same keys.

Keys are wire format — they ride in callback data, start payloads, and the
pool_strategy_subs table — so they must never be renamed casually.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bot_config
import config

logger = logging.getLogger(__name__)

ICT = "ict"
MILL = "mill"
KALSHI_REVERSAL = "kalshi_reversal"
KALSHI_WICK = "kalshi_wick"


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    # One-line pitch shown on the /subscribe picker.
    pitch: str
    # Risk copy shown on the allocation prompt.
    risk_lines: str
    # Whether Accept on this lane can reach an executor today. The Kalshi
    # lanes publish cards only — capital does not route to them yet.
    executable: bool


STRATEGIES: dict[str, Strategy] = {
    ICT: Strategy(
        key=ICT,
        label="ICT Trades",
        pitch=(
            "Eva's flagship ICT desk — multi-timeframe structure reads on ETH "
            "and BTC, a few high-conviction trades a week."
        ),
        risk_lines=(
            "• Each Accept risks about "
            f"{bot_config.POOL_RISK_PCT * 100:.1f}% of your allocation to this "
            "strategy at the stop — never your full allocation.\n"
            "• Hard stop and staged take-profits on every position; exits are "
            "automatic.\n"
            "• Typically a handful of trades a week, held hours to days."
        ),
        executable=True,
    ),
    MILL: Strategy(
        key=MILL,
        label="Trade Mill",
        pitch=(
            "The idea stream — Eva's brain constantly minting structured "
            "intraday setups from news, stance flips and volatility."
        ),
        risk_lines=(
            "• Each Accept risks about "
            f"{bot_config.POOL_RISK_PCT * 100:.1f}% of your allocation to this "
            "strategy at the stop.\n"
            "• Many small ideas a day; fixed clip sizes under daily-loss and "
            "concurrency halts.\n"
            "• Intraday holds — most ideas resolve within hours."
        ),
        executable=True,
    ),
    KALSHI_REVERSAL: Strategy(
        key=KALSHI_REVERSAL,
        label="Kalshi 15m Reversal",
        pitch=(
            "Event-market lane — fades stretched runs of 15-minute candles on "
            "Kalshi, settled every quarter hour."
        ),
        risk_lines=(
            "• Contracts settle every 15 minutes — small, fast, capped "
            "per-window risk.\n"
            "• Idea cards only for now: capital deployment to Kalshi is "
            "coming soon, so Accept does not place an order yet."
        ),
        executable=False,
    ),
    KALSHI_WICK: Strategy(
        key=KALSHI_WICK,
        label="Kalshi 15m Wick",
        pitch=(
            "Event-market lane — buys the late-window favourite on Kalshi's "
            "15-minute markets and holds to settlement."
        ),
        risk_lines=(
            "• One entry per 15-minute window, held to settlement — capped "
            "per-window risk.\n"
            "• Idea cards only for now: capital deployment to Kalshi is "
            "coming soon, so Accept does not place an order yet."
        ),
        executable=False,
    ),
}

# Fixed display order everywhere the strategies are listed.
ORDER = (ICT, MILL, KALSHI_REVERSAL, KALSHI_WICK)


def get(key: str) -> Strategy | None:
    return STRATEGIES.get(key)


def is_valid(key: str) -> bool:
    return key in STRATEGIES


# ---------------------------------------------------------------------------
# Live P&L lines for the allocation prompt. Best-effort reads of the local
# ledgers; a missing table or db yields a graceful omission, never a crash —
# the prompt is copy, not accounting.
# ---------------------------------------------------------------------------


def _ro(path: str | Path) -> sqlite3.Connection | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _hub_live_line(source: str, sleeve: float, label: str) -> str | None:
    conn = _ro(config.LEDGER_DB)
    if conn is None or not sleeve:
        return None
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(COALESCE(realized_pnl_usd, pnl_usd, 0)) pnl"
            " FROM live_trades WHERE source = ? AND status = 'closed'",
            (source,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or not row["n"]:
        return None
    pct = float(row["pnl"] or 0) / sleeve * 100
    return f"• {label}: {pct:+.2f}% over {int(row['n'])} closed live trades."


_KALSHI_EPOCHS = {KALSHI_REVERSAL: ("eva_streak", "2026-09-14"),
                  KALSHI_WICK: ("eva_wick", "2026-09-17")}


def _kalshi_live_line(key: str) -> str | None:
    try:
        import kalshi_bridge

        db = kalshi_bridge.kalshi_db_path()
    except Exception:  # noqa: BLE001 — bridge not configured on this box
        return None
    conn = _ro(db) if db else None
    if conn is None:
        return None
    bot_id, epoch = _KALSHI_EPOCHS[key]
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(pnl_usd) pnl,"
            " SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins"
            " FROM paper_positions WHERE bot_id = ? AND status != 'open'"
            " AND closed_at >= ?",
            (bot_id, epoch),
        ).fetchone()
        state = conn.execute(
            "SELECT starting_usd FROM paper_state WHERE bot_id = ?", (bot_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or not row["n"]:
        return f"• Current rules live since {epoch}; first settled windows are still coming in."
    bank = float(state["starting_usd"]) if state else 0.0
    pct = (float(row["pnl"] or 0) / bank * 100) if bank else None
    wr = float(row["wins"] or 0) / float(row["n"]) * 100
    parts = [f"{wr:.0f}% win rate over {int(row['n'])} settled since {epoch}"]
    if pct is not None:
        parts.insert(0, f"{pct:+.2f}%")
    return "• Live record: " + ", ".join(parts) + "."


def live_pnl_line(key: str) -> str | None:
    """One live-results line for the allocation prompt, or None."""
    try:
        if key == ICT:
            return _hub_live_line(
                "hq", float(bot_config.LIVE_HQ_EQUITY_USD), "Live book"
            )
        if key == MILL:
            return _hub_live_line(
                "mill",
                float(getattr(bot_config, "LIVE_MILL_SLEEVE_USD", 0) or 0),
                "Live book",
            )
        if key in (KALSHI_REVERSAL, KALSHI_WICK):
            return _kalshi_live_line(key)
    except Exception:  # noqa: BLE001
        logger.exception("live_pnl_line failed for %s", key)
    return None


def allocation_prompt(key: str, portfolio: dict[str, Any],
                      current_alloc: float = 0.0) -> str:
    """The 'do you want to allocate now?' message for one strategy."""
    strat = STRATEGIES[key]
    cash = float(portfolio.get("cash_usd") or 0)
    available = float(portfolio.get("available_usd", cash) or 0)
    lines = [
        f"Allocate to {strat.label}?",
        "",
        "You're subscribed — idea cards from this strategy will arrive here "
        "either way. Allocating is what lets an Accept put money on one.",
        "",
        f"Your cash: ${cash:,.2f} (${available:,.2f} not yet reserved)",
    ]
    if current_alloc > 0:
        lines.append(f"Currently allocated here: ${current_alloc:,.2f}")
    lines += ["", "Risk profile:", strat.risk_lines]
    live = live_pnl_line(key)
    if live:
        lines.append(live)
    if not strat.executable:
        lines += [
            "",
            "Note: this lane publishes idea cards only for now — allocation "
            "will activate once Kalshi execution ships.",
        ]
    lines += [
        "",
        "Pick how much of your available cash to allocate, or skip for now — "
        "you can always allocate later with /allocate "
        f"{key} <amount>.",
    ]
    return "\n".join(lines)


def deploy_prompt(key: str, portfolio: dict[str, Any]) -> str:
    """Shown when an Accept lands on a strategy with no allocation."""
    strat = STRATEGIES[key]
    cash = float(portfolio.get("cash_usd") or 0)
    available = float(portfolio.get("available_usd", cash) or 0)
    lines = [
        f"You haven't deployed capital to {strat.label} yet, so this Accept "
        "wasn't placed.",
        "",
        f"Your cash: ${cash:,.2f} (${available:,.2f} not yet reserved)",
        "",
        "Risk profile:",
        strat.risk_lines,
    ]
    live = live_pnl_line(key)
    if live:
        lines.append(live)
    lines += [
        "",
        "Allocate below (or /allocate "
        f"{key} <amount>), then Accept the next card to get in.",
    ]
    return "\n".join(lines)
