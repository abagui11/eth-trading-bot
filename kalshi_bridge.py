"""Read-only bridge to the colocated Kalshi 15m bot ledger.

The Kalshi 15m bots (kalshi_15m_bot repo, /opt/kalshi-15m-bot on the VPS)
keep their books in one SQLite ledger. This module mirrors the
trade_ideas_bridge pattern: fail-soft reads over ``KALSHI_DB`` so the hub
dashboard can show bot performance without importing bot code.

Hub .env knobs:

* ``KALSHI_DB=/opt/kalshi-15m-bot/ledger.db`` — required for the tab.
* ``KALSHI_LIVE_BOTS=eva_streak`` — which bot(s) trade the real account;
  everything else shows as PAPER. Mirrors the bot repo's env of the same name.
* ``KALSHI_EXPERIMENT_EPOCH`` — comparison start line (default: the
  2026-09-08 multi-bot flip). All per-bot stats and the closed list count
  from here so live and paper books race from the same start.
* ``KALSHI_LASTMIN_DB=/opt/kalshi-15m-bot/lastmin.db`` — optional; enables
  the Eva #3 last-2-min arb logger card (logging only, no trading).

When ``KALSHI_DB`` is unset or unreadable every payload reports
``{"available": False}`` and the tab shows a mount hint instead of breaking.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Kalshi 15m products trade on ET walls; the bot's boss rules are ET-based.
_ET = timezone(timedelta(hours=-4))

_BOT_LABELS = {
    "control": "Control (conviction ICT)",
    "lottery": "Lottery / hail-mary",
    "adverse": "Adverse / wick-hunt",
    "eva_wick": "EVA wick",
    "eva_streak": "EVA reversal",
    "eva_arb": "EVA arb",
}

# Short grey subtitles under each bot name in the comparison table (≤4 lines).
_BOT_BLURBS = {
    "eva_streak": (
        "After ≥3 same-direction 15m candles with a sweep of the prior extreme, "
        "buy the opposite side at the open mid only when priced 45–65¢. "
        "Cash out at 2× or cut at ½; cool down after consecutive stops."
    ),
    "eva_wick": (
        "Join the move the old wick fade bet against: same session-range pop/"
        "flush triggers and EVA stance gates, but buy the momentum side at "
        "~67–80¢ and hold to settlement. Replaced 2026-09-16; epoch book "
        "backfilled from the replayed inverse — treat as a forward paper test."
    ),
    "eva_arb": (
        "Last 2 minutes only. If the favorite touched 90¢ then dips to 75–85¢, "
        "buy the favored side before quotes freeze and hold to settlement. "
        "Paper trading this epoch."
    ),
}

# Bots always shown in the comparison, even before their first trade.
_EXPERIMENT_BOTS = ("eva_streak", "eva_wick", "eva_arb")

# Multi-bot experiment flip: eva_streak went live (mid entry), eva_wick moved
# to paper with the double-down rule. Comparison starts here.
_EXPERIMENT_EPOCH_DEFAULT = "2026-09-08T18:00:00Z"


def experiment_epoch() -> str:
    return (
        os.getenv("KALSHI_EXPERIMENT_EPOCH") or _EXPERIMENT_EPOCH_DEFAULT
    ).strip()


def live_bots() -> tuple[str, ...]:
    raw = (os.getenv("KALSHI_LIVE_BOTS") or "eva_streak").strip()
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def kalshi_db_path() -> Path | None:
    raw = (os.getenv("KALSHI_DB") or "").strip()
    return Path(raw) if raw else None


def lastmin_db_path() -> Path | None:
    raw = (os.getenv("KALSHI_LASTMIN_DB") or "").strip()
    return Path(raw) if raw else None


def enabled() -> bool:
    path = kalshi_db_path()
    return path is not None and path.exists()


def _connect(path: Path | None) -> sqlite3.Connection | None:
    if path is None or not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        logger.exception("Kalshi ledger unavailable at %s", path)
        return None


def _fmt_ts(value: Any) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:16]
    return dt.astimezone(_ET).strftime("%m/%d %H:%M")


def _position_row(row: sqlite3.Row) -> dict[str, Any]:
    entry = float(row["entry_cents"] or 0)
    pnl = row["pnl_usd"]
    return {
        "id": int(row["id"]),
        "bot_id": str(row["bot_id"] or "control"),
        "opened_at": _fmt_ts(row["opened_at"]),
        "closed_at": _fmt_ts(row["closed_at"]),
        "market_ticker": str(row["market_ticker"] or ""),
        "product_id": str(row["product_id"] or ""),
        "side": str(row["side"] or ""),
        "contracts": int(row["contracts"] or 0),
        "entry_cents": entry,
        "cost_usd": entry / 100.0 * int(row["contracts"] or 0),
        "result": str(row["result"] or ""),
        "pnl_usd": float(pnl) if pnl is not None else None,
        "rationale": str(row["rationale"] or ""),
    }


def lastmin_payload(max_windows: int = 400) -> dict[str, Any] | None:
    """Eva #3 arb logger evidence: dip setups seen vs how they settled.

    A "dip setup" = the favored side touched >=90c inside the final window
    and later printed back inside 75-85c. No trading — this only answers
    "how often would that buy have settled in the money?".
    """
    conn = _connect(lastmin_db_path())
    if conn is None:
        return None
    try:
        results = conn.execute(
            "SELECT ticker, result FROM results ORDER BY settled_ts DESC LIMIT ?",
            (int(max_windows),),
        ).fetchall()
        windows_total = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        quotes_total = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        dips = 0
        dip_wins = 0
        for r in results:
            rows = conn.execute(
                "SELECT yes_mid FROM quotes WHERE ticker = ? AND yes_mid IS NOT NULL"
                " ORDER BY id",
                (str(r["ticker"]),),
            ).fetchall()
            mids = [float(q["yes_mid"]) for q in rows]
            if not mids:
                continue
            for favored, touch, lo, hi in (
                ("yes", lambda m: m >= 90.0, 75.0, 85.0),
                ("no", lambda m: m <= 10.0, 15.0, 25.0),
            ):
                touched = False
                dipped = False
                for m in mids:
                    if touch(m):
                        touched = True
                    elif touched and lo <= m <= hi:
                        dipped = True
                        break
                if dipped:
                    dips += 1
                    if str(r["result"]) == favored:
                        dip_wins += 1
                    break
    except sqlite3.Error:
        logger.exception("lastmin db query failed")
        return None
    finally:
        conn.close()
    return {
        "windows": int(windows_total or 0),
        "quotes": int(quotes_total or 0),
        "dips": dips,
        "dip_wins": dip_wins,
        "dip_win_rate": (dip_wins / dips) if dips else None,
    }


def performance_payload(limit: int = 15) -> dict[str, Any] | None:
    """Kalshi multi-bot snapshot for the hub tab; None when not mounted."""
    conn = _connect(kalshi_db_path())
    if conn is None:
        return None
    epoch = experiment_epoch()
    try:
        states = conn.execute(
            "SELECT bot_id, starting_usd, cash_usd, realized_pnl_usd"
            " FROM paper_state ORDER BY bot_id"
        ).fetchall()
        open_rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status = 'open'"
            " ORDER BY opened_at DESC LIMIT 40"
        ).fetchall()
        closed_rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status != 'open'"
            " AND opened_at >= ? ORDER BY closed_at DESC LIMIT ?",
            (epoch, max(1, min(int(limit), 100))),
        ).fetchall()
        agg = conn.execute(
            "SELECT bot_id,"
            "  COUNT(*) AS closed,"
            "  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,"
            "  SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) AS losses,"
            "  SUM(COALESCE(pnl_usd, 0)) AS pnl_usd,"
            "  SUM(CASE WHEN result = 'flat' THEN 1 ELSE 0 END) AS early_exits"
            " FROM paper_positions"
            " WHERE status != 'open' AND opened_at >= ?"
            " GROUP BY bot_id",
            (epoch,),
        ).fetchall()
        hidden_n = conn.execute(
            "SELECT COUNT(*) FROM paper_positions"
            " WHERE status != 'open' AND opened_at < ?",
            (epoch,),
        ).fetchone()[0]
    except sqlite3.Error:
        logger.exception("Kalshi ledger query failed")
        return None
    finally:
        conn.close()

    live_set = set(live_bots())
    open_list = [_position_row(r) for r in open_rows]
    closed_list = [_position_row(r) for r in closed_rows]
    for p in open_list + closed_list:
        p["mode"] = "live" if p["bot_id"] in live_set else "paper"
    agg_by_bot = {str(r["bot_id"]): r for r in agg}
    open_cost_by_bot: dict[str, float] = {}
    for pos in open_list:
        open_cost_by_bot[pos["bot_id"]] = (
            open_cost_by_bot.get(pos["bot_id"], 0.0) + pos["cost_usd"]
        )

    bots: list[dict[str, Any]] = []
    for st in states:
        bot_id = str(st["bot_id"])
        a = agg_by_bot.get(bot_id)
        closed = int(a["closed"]) if a else 0
        wins = int(a["wins"] or 0) if a else 0
        losses = int(a["losses"] or 0) if a else 0
        n_open = sum(1 for p in open_list if p["bot_id"] == bot_id)
        # Idle leftover books (old control/lottery rows) stay off the tab.
        if closed == 0 and n_open == 0 and bot_id not in _EXPERIMENT_BOTS:
            continue
        decided = wins + losses
        cash = float(st["cash_usd"] or 0)
        bots.append(
            {
                "bot_id": bot_id,
                "label": _BOT_LABELS.get(bot_id, bot_id),
                "blurb": _BOT_BLURBS.get(bot_id, ""),
                "mode": "live" if bot_id in live_set else "paper",
                "starting_usd": float(st["starting_usd"] or 0),
                "cash_usd": cash,
                "equity_usd": cash + open_cost_by_bot.get(bot_id, 0.0),
                "realized_pnl_usd": float(st["realized_pnl_usd"] or 0),
                "epoch_pnl_usd": float(a["pnl_usd"] or 0) if a else 0.0,
                "open": n_open,
                "closed": closed,
                "wins": wins,
                "losses": losses,
                "early_exits": int(a["early_exits"] or 0) if a else 0,
                "win_rate": (wins / decided) if decided else None,
            }
        )
    # Live book first, then paper books alphabetically.
    bots.sort(key=lambda b: (b["mode"] != "live", b["bot_id"]))

    live_list = [b for b in bots if b["mode"] == "live"]
    live_wins = sum(b["wins"] for b in live_list)
    live_losses = sum(b["losses"] for b in live_list)
    decided = live_wins + live_losses
    totals = {
        "label": " + ".join(b["label"] for b in live_list) or "(no live bot)",
        "starting_usd": sum(b["starting_usd"] for b in live_list),
        "equity_usd": sum(b["equity_usd"] for b in live_list),
        "realized_pnl_usd": sum(b["realized_pnl_usd"] for b in live_list),
        "epoch_pnl_usd": sum(b["epoch_pnl_usd"] for b in live_list),
        "open": sum(b["open"] for b in live_list),
        "closed": sum(b["closed"] for b in live_list),
        "wins": live_wins,
        "losses": live_losses,
        "win_rate": (live_wins / decided) if decided else None,
    }
    return {
        "available": True,
        "experiment_epoch": epoch,
        "experiment_epoch_label": _fmt_ts(epoch) + " ET",
        "live_bots": sorted(live_set),
        "hidden_closed": int(hidden_n or 0),
        "totals": totals,
        "bots": bots,
        "open": open_list,
        "closed": closed_list,
        "lastmin": lastmin_payload(),
    }
