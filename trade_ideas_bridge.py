"""Bridge to the colocated trade_ideas mill database.

The mill mints the public volume ideas and sends their cards through THIS
bot's token (send-only). Telegram allows a single getUpdates consumer per
token and that consumer is this process, so the Accept/Reject callbacks land
here and are written back into the mill's SQLite.

Read-mostly and fail-soft: when IDEAS_DB is unset or the mill has not created
its database yet, every call reports "unavailable" and the bot carries on.

Accept also opens ``user_paper_trades`` (personal portfolio) using the idea's
sized Entry/SL/TP1 — mirroring trade_ideas.store.record_decision.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bot_config

logger = logging.getLogger(__name__)

DecisionStatus = str  # recorded | duplicate | unknown_idea | unavailable

_USER_PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry REAL,
    stop_loss REAL,
    take_profit REAL,
    take_profits_json TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    exit_price REAL,
    pnl_pct REAL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE (idea_id, user_id),
    FOREIGN KEY (idea_id) REFERENCES ideas(id)
);
CREATE INDEX IF NOT EXISTS idx_user_paper_user ON user_paper_trades(user_id);
CREATE INDEX IF NOT EXISTS idx_user_paper_status ON user_paper_trades(status);
"""


def ideas_db_path() -> Path | None:
    raw = (os.getenv("IDEAS_DB") or "").strip()
    if not raw:
        return None
    return Path(raw)


def enabled() -> bool:
    path = ideas_db_path()
    return path is not None and path.exists()


def _connect() -> sqlite3.Connection | None:
    path = ideas_db_path()
    if path is None or not path.exists():
        return None
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def get_idea(idea_id: int) -> dict[str, Any] | None:
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn:
            row = conn.execute(
                "SELECT id, source, product_id, direction, title FROM ideas WHERE id = ?",
                (idea_id,),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        logger.exception("trade_ideas bridge read failed for idea %s", idea_id)
        return None
    finally:
        conn.close()


def _open_user_paper(
    conn: sqlite3.Connection,
    idea_id: int,
    user_id: int,
    opened_at: str,
) -> None:
    """Open a personal paper row from idea levels (Accept path). Fail-soft."""
    conn.executescript(_USER_PAPER_SCHEMA)
    row = conn.execute(
        """
        SELECT product_id, direction, entry, stop_loss, take_profits_json
        FROM ideas WHERE id = ?
        """,
        (idea_id,),
    ).fetchone()
    if row is None:
        return
    direction = str(row["direction"] or "")
    entry = row["entry"]
    stop = row["stop_loss"]
    if direction not in ("long", "short") or entry is None or stop is None:
        return
    tps: list[float] = []
    raw_tps = row["take_profits_json"]
    if raw_tps:
        try:
            parsed = json.loads(str(raw_tps))
            tps = [float(x) for x in parsed]
        except (json.JSONDecodeError, TypeError, ValueError):
            tps = []
    tp1 = float(tps[0]) if tps else None
    try:
        conn.execute(
            """
            INSERT INTO user_paper_trades
                (idea_id, user_id, product_id, direction, entry, stop_loss,
                 take_profit, take_profits_json, status, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                int(idea_id),
                int(user_id),
                str(row["product_id"]),
                direction,
                float(entry),
                float(stop),
                tp1,
                json.dumps(tps),
                opened_at,
            ),
        )
    except sqlite3.IntegrityError:
        return


def record_decision(idea_id: int, user_id: int, decision: str) -> DecisionStatus:
    """Write an Accept/Reject into the mill's decisions table.

    Accept also opens ``user_paper_trades`` when the idea has sized levels.
    """
    if decision not in ("accept", "reject"):
        raise ValueError(f"invalid decision: {decision}")

    conn = _connect()
    if conn is None:
        return "unavailable"
    try:
        with conn:
            exists = conn.execute(
                "SELECT 1 FROM ideas WHERE id = ? LIMIT 1", (idea_id,)
            ).fetchone()
            if exists is None:
                return "unknown_idea"
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                conn.execute(
                    """
                    INSERT INTO decisions (idea_id, user_id, decision, decided_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (idea_id, user_id, decision, now),
                )
            except sqlite3.IntegrityError:
                return "duplicate"
            if decision == "accept":
                _open_user_paper(conn, idea_id, user_id, now)
        return "recorded"
    except sqlite3.Error:
        logger.exception("trade_ideas bridge write failed for idea %s", idea_id)
        return "unavailable"
    finally:
        conn.close()


def is_fill_operator(user_id: int) -> bool:
    """True when this Telegram id's Accept fills a real mill clip."""
    return int(user_id) in tuple(bot_config.LIVE_MILL_FILL_TELEGRAM_IDS)


def _mark_idea_live_fill(
    conn: sqlite3.Connection, idea_id: int, fill_type: str, filled_by: int | None
) -> None:
    """Record on the mill's idea row how its live clip was entered.

    The mill owns this schema; the columns are added defensively here because
    a hub deploy can land before the mill restarts and runs its migration.
    """
    existing = {
        str(r[1]) for r in conn.execute("PRAGMA table_info(ideas)").fetchall()
    }
    if "live_fill_type" not in existing:
        conn.execute("ALTER TABLE ideas ADD COLUMN live_fill_type TEXT")
    if "live_filled_by" not in existing:
        conn.execute("ALTER TABLE ideas ADD COLUMN live_filled_by INTEGER")
    conn.execute(
        "UPDATE ideas SET live_fill_type = ?, live_filled_by = ? WHERE id = ?",
        (fill_type, int(filled_by) if filled_by is not None else None, int(idea_id)),
    )


def request_manual_fill(idea_id: int, user_id: int) -> dict[str, Any]:
    """Fill a real mill clip from an operator's Accept.

    Blocking (SQLite + Coinbase REST) — call it off the event loop. Fail-soft:
    any error reports a skip so the Accept itself still succeeds.
    """
    if not is_fill_operator(user_id):
        return {"executed": False, "skip_reason": "not_authorized"}

    conn = _connect()
    if conn is None:
        return {"executed": False, "skip_reason": "unavailable"}
    try:
        with conn:
            row = conn.execute(
                """
                SELECT product_id, direction, entry, stop_loss,
                       take_profits_json, signal_key, confidence
                FROM ideas WHERE id = ?
                """,
                (int(idea_id),),
            ).fetchone()
    except sqlite3.Error:
        logger.exception("manual fill idea read failed for #%s", idea_id)
        conn.close()
        return {"executed": False, "skip_reason": "unavailable"}

    try:
        if row is None:
            return {"executed": False, "skip_reason": "unknown_idea"}
        if row["entry"] is None or row["stop_loss"] is None:
            return {"executed": False, "skip_reason": "unsized"}

        import execute

        verdict = execute.execute_mill_idea(
            idea_id=int(idea_id),
            product_id=str(row["product_id"]),
            direction=str(row["direction"] or ""),
            entry=float(row["entry"]),
            stop_loss=float(row["stop_loss"]),
            take_profits=_parse_take_profits(row["take_profits_json"]),
            signal_key=str(row["signal_key"] or "") or None,
            confidence=row["confidence"],
            fill_type="manual",
            accepted_by=int(user_id),
        )
        if verdict.get("executed"):
            try:
                with conn:
                    _mark_idea_live_fill(conn, int(idea_id), "manual", int(user_id))
            except sqlite3.Error:
                logger.exception("manual fill idea tag failed for #%s", idea_id)
        return verdict
    except Exception:
        logger.exception("manual fill failed for idea #%s", idea_id)
        return {"executed": False, "skip_reason": "error"}
    finally:
        conn.close()


def format_manual_fill_reply(verdict: dict[str, Any], idea_id: int) -> str | None:
    """Operator-facing note about the live clip. None = stay quiet."""
    capacity = verdict.get("capacity") or {}
    open_n = capacity.get("open")
    max_open = capacity.get("max_open")

    if verdict.get("executed"):
        result = verdict.get("result") or {}
        mode = str(result.get("mode") or "")
        qty = result.get("qty")
        notional = result.get("notional_usd")
        head = "Live clip placed" if mode == "live" else f"Live clip ({mode})"
        bits = [f"{head} for idea #{idea_id}"]
        if qty is not None and notional is not None:
            bits.append(f"{float(qty):g} @ ~${float(notional):,.0f}")
        if open_n is not None and max_open is not None:
            bits.append(f"sleeve {open_n}/{max_open}")
        return " · ".join(bits)

    reason = str(verdict.get("skip_reason") or "")
    if reason == "sleeve_full":
        lines = [
            f"Too many trades open — idea #{idea_id} was NOT filled.",
            f"The mill sleeve is full at {open_n}/{max_open} positions.",
        ]
        open_trades = capacity.get("open_trades") or []
        if open_trades:
            lines.append("")
            lines.append("Currently open:")
            for t in open_trades:
                lines.append(
                    f"  #{t.get('id')} {t.get('product_id')} {t.get('side')} "
                    f"@ {_fmt_px(t.get('entry'))} · {t.get('fill_type')}"
                )
            lines.append("")
            lines.append("Close one to free a slot, then Accept again.")
        return "\n".join(lines)
    if reason == "halted":
        return (
            f"Idea #{idea_id} was NOT filled — live trading is halted "
            f"({capacity.get('halted')})."
        )
    if reason == "unsized":
        return f"Idea #{idea_id} has no entry/stop yet — no live clip placed."
    if reason == "rejected":
        return (
            f"Idea #{idea_id} was accepted but the live clip did not go through "
            "(exposure, contract floor, or execution mode). Check the agent log."
        )
    if reason in ("unavailable", "error"):
        return f"Couldn’t reach the live sleeve for idea #{idea_id} — check the log."
    return None


def format_decision_reply(status: DecisionStatus, decision: str, idea_id: int) -> str:
    if status == "recorded":
        if decision == "accept":
            return (
                f"Accepted idea #{idea_id}. Added to your personal paper book — /me"
            )
        return f"Rejected idea #{idea_id}. Logged to your idea history."
    if status == "duplicate":
        return f"You already decided on idea #{idea_id}."
    if status == "unknown_idea":
        return f"Idea #{idea_id} is no longer available."
    return "Idea decisions are unavailable right now — try again shortly."


def volume_book_payload(*, limit: int = 100) -> dict[str, Any] | None:
    """Hidden /volume page data: every paper trade + idea metadata."""
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn:
            # paper_trades may not exist yet on older DBs
            tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "paper_trades" not in tables:
                ideas = conn.execute(
                    """
                    SELECT id, source, product_id, direction, title, confidence,
                           status, entry, stop_loss, take_profits_json, created_at, sent_at
                    FROM ideas
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return {
                    "available": True,
                    "summary": {"open": 0, "closed": 0, "win_rate": None, "pnl_pct_sum": 0.0},
                    "trades": [],
                    "ideas": [dict(r) for r in ideas],
                }

            trades = conn.execute(
                """
                SELECT p.*, i.title, i.source, i.confidence, i.status AS idea_status,
                       i.sent_at, i.blurb
                FROM paper_trades p
                LEFT JOIN ideas i ON i.id = p.idea_id
                ORDER BY p.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            status_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n, COALESCE(SUM(pnl_pct), 0) AS pnl
                FROM paper_trades GROUP BY status
                """
            ).fetchall()
        by_status = {
            str(r["status"]): {"count": int(r["n"]), "pnl_pct_sum": float(r["pnl"])}
            for r in status_rows
        }
        closed = by_status.get("hit_tp", {"count": 0})["count"] + by_status.get(
            "hit_sl", {"count": 0}
        )["count"]
        wins = by_status.get("hit_tp", {"count": 0})["count"]
        return {
            "available": True,
            "summary": {
                "by_status": by_status,
                "open": by_status.get("open", {"count": 0})["count"],
                "closed": closed,
                "win_rate": (wins / closed) if closed else None,
                "pnl_pct_sum": sum(v["pnl_pct_sum"] for v in by_status.values()),
            },
            "trades": [dict(r) for r in trades],
            "ideas": [],
        }
    except sqlite3.Error:
        logger.exception("volume book read failed")
        return None
    finally:
        conn.close()


def user_book_payload(user_id: int, *, limit: int = 100) -> dict[str, Any] | None:
    """Personal accepted-idea paper book for Telegram /me."""
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn:
            tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "user_paper_trades" not in tables:
                return {
                    "available": True,
                    "summary": {
                        "by_status": {},
                        "open": 0,
                        "closed": 0,
                        "win_rate": None,
                        "pnl_pct_sum": 0.0,
                    },
                    "trades": [],
                    "user_id": int(user_id),
                }

            trades = conn.execute(
                """
                SELECT p.*, i.title, i.source, i.confidence, i.status AS idea_status,
                       i.sent_at, i.blurb
                FROM user_paper_trades p
                LEFT JOIN ideas i ON i.id = p.idea_id
                WHERE p.user_id = ?
                ORDER BY p.id DESC
                LIMIT ?
                """,
                (int(user_id), limit),
            ).fetchall()
            status_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n, COALESCE(SUM(pnl_pct), 0) AS pnl
                FROM user_paper_trades
                WHERE user_id = ?
                GROUP BY status
                """,
                (int(user_id),),
            ).fetchall()
        by_status = {
            str(r["status"]): {"count": int(r["n"]), "pnl_pct_sum": float(r["pnl"])}
            for r in status_rows
        }
        tp_n = by_status.get("hit_tp", {"count": 0})["count"]
        sl_n = by_status.get("hit_sl", {"count": 0})["count"]
        manual_n = by_status.get("manual", {"count": 0})["count"]
        auto_closed = tp_n + sl_n
        closed = auto_closed + manual_n
        return {
            "available": True,
            "summary": {
                "by_status": by_status,
                "open": by_status.get("open", {"count": 0})["count"],
                "closed": closed,
                "win_rate": (tp_n / auto_closed) if auto_closed else None,
                "pnl_pct_sum": sum(v["pnl_pct_sum"] for v in by_status.values()),
            },
            "trades": [dict(r) for r in trades],
            "user_id": int(user_id),
        }
    except sqlite3.Error:
        logger.exception("user book read failed for user %s", user_id)
        return None
    finally:
        conn.close()


def _unrealized_pct(direction: str, entry: float, spot: float) -> float:
    if direction == "long":
        return (spot - entry) / entry * 100.0
    if direction == "short":
        return (entry - spot) / entry * 100.0
    return 0.0


def _fmt_pct(value: float | None, *, signed: bool = True) -> str:
    if value is None:
        return "n/a"
    if signed:
        return f"{value:+.2f}%"
    return f"{value:.2f}%"


def _fmt_px(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(float(value)) >= 1000:
        return f"{float(value):,.2f}"
    return f"{float(value):.2f}"


def _enrich_book_report(
    payload: dict[str, Any],
    spots: dict[str, float],
    *,
    open_limit: int,
    closed_limit: int,
    lane: str,
    label: str,
) -> dict[str, Any]:
    summary = dict(payload.get("summary") or {})
    open_trades: list[dict[str, Any]] = []
    closed_trades: list[dict[str, Any]] = []
    for raw in payload.get("trades") or []:
        trade = dict(raw)
        status = str(trade.get("status") or "")
        if status == "open":
            product = str(trade.get("product_id") or "")
            spot = spots.get(product)
            entry = trade.get("entry")
            direction = str(trade.get("direction") or "")
            stop = trade.get("stop_loss")
            tp = trade.get("take_profit")
            trade["spot"] = spot
            trade["unrealized_pct"] = None
            trade["to_sl_pct"] = None
            trade["to_tp1_pct"] = None
            if spot is not None and entry is not None:
                entry_f = float(entry)
                spot_f = float(spot)
                trade["unrealized_pct"] = round(
                    _unrealized_pct(direction, entry_f, spot_f), 4
                )
                if stop is not None:
                    trade["to_sl_pct"] = round(
                        (float(stop) - spot_f) / spot_f * 100.0, 4
                    )
                if tp is not None:
                    trade["to_tp1_pct"] = round(
                        (float(tp) - spot_f) / spot_f * 100.0, 4
                    )
            open_trades.append(trade)
        elif status in ("hit_tp", "hit_sl", "manual"):
            closed_trades.append(trade)

    unrealized_sum = sum(
        float(t["unrealized_pct"])
        for t in open_trades
        if t.get("unrealized_pct") is not None
    )
    realized = float(summary.get("pnl_pct_sum") or 0.0)
    out: dict[str, Any] = {
        "available": True,
        **summary,
        "spots": spots,
        "unrealized_pct_sum": round(unrealized_sum, 4),
        "total_pnl_pct_sum": round(realized + unrealized_sum, 4),
        "open_trades": open_trades[:open_limit],
        "open_trades_total": len(open_trades),
        "recent_closed": closed_trades[:closed_limit],
        "lane": lane,
        "label": label,
    }
    if "user_id" in payload:
        out["user_id"] = payload["user_id"]
    return out


def volume_book_report(
    spots: dict[str, float] | None = None,
    *,
    open_limit: int = 12,
    closed_limit: int = 8,
) -> dict[str, Any] | None:
    """Volume-lane book with unrealized mark-to-market (Telegram /performance)."""
    payload = volume_book_payload(limit=max(open_limit + closed_limit, 100))
    if payload is None:
        return None
    return _enrich_book_report(
        payload,
        spots or {},
        open_limit=open_limit,
        closed_limit=closed_limit,
        lane="volume",
        label="Volume idea book (public mill)",
    )


def user_book_report(
    user_id: int,
    spots: dict[str, float] | None = None,
    *,
    open_limit: int = 12,
    closed_limit: int = 8,
) -> dict[str, Any] | None:
    """Personal accepted-idea book (Telegram /me)."""
    payload = user_book_payload(user_id, limit=max(open_limit + closed_limit, 100))
    if payload is None:
        return None
    return _enrich_book_report(
        payload,
        spots or {},
        open_limit=open_limit,
        closed_limit=closed_limit,
        lane="personal",
        label="Your idea portfolio",
    )


def _format_book_body(
    report: dict[str, Any],
    *,
    max_open_lines: int,
    empty_closed_hint: str,
) -> list[str]:
    open_n = int(report.get("open") or 0)
    closed_n = int(report.get("closed") or 0)
    win_rate = report.get("win_rate")
    wr = f"{win_rate * 100:.0f}%" if isinstance(win_rate, (int, float)) else "n/a"
    by = report.get("by_status") or {}
    tp_n = int((by.get("hit_tp") or {}).get("count") or 0)
    sl_n = int((by.get("hit_sl") or {}).get("count") or 0)
    manual_n = int((by.get("manual") or {}).get("count") or 0)
    closed_bits = f"TP {tp_n} / SL {sl_n}"
    if manual_n:
        closed_bits += f" / manual {manual_n}"

    lines = [
        report.get("label") or "Idea book",
        "",
        f"Open {open_n} · Closed {closed_n} ({closed_bits})",
        f"Win rate {wr}",
        f"Realized PnL  {_fmt_pct(report.get('pnl_pct_sum'))}",
        f"Unrealized    {_fmt_pct(report.get('unrealized_pct_sum'))}",
        f"Total (mark)  {_fmt_pct(report.get('total_pnl_pct_sum'))}",
    ]

    spots = report.get("spots") or {}
    if spots:
        spot_bits = [
            f"{pid.replace('-USD', '')} {_fmt_px(px)}"
            for pid, px in sorted(spots.items())
        ]
        lines.append("Spot " + " · ".join(spot_bits))

    open_trades = list(report.get("open_trades") or [])
    shown = open_trades[:max_open_lines]
    if shown:
        lines.extend(["", "Open positions"])
        for t in shown:
            src = t.get("source") or "?"
            title = (t.get("title") or "").strip()
            label = title[:42] if title else f"idea #{t.get('idea_id')}"
            lines.append(
                f"#{t.get('id')} {t.get('product_id')} {t.get('direction')} · {src} · "
                f"uPnL {_fmt_pct(t.get('unrealized_pct'))}"
            )
            lines.append(
                f"  entry {_fmt_px(t.get('entry'))}  "
                f"TP1 {_fmt_px(t.get('take_profit'))} ({_fmt_pct(t.get('to_tp1_pct'))})  "
                f"SL {_fmt_px(t.get('stop_loss'))} ({_fmt_pct(t.get('to_sl_pct'))})"
            )
            lines.append(f"  {label}")
        total_open = int(report.get("open_trades_total") or open_n)
        if total_open > len(shown):
            lines.append(f"  … +{total_open - len(shown)} more open")

    closed = list(report.get("recent_closed") or [])
    if closed:
        lines.extend(["", "Recent closed"])
        for t in closed[:5]:
            lines.append(
                f"#{t.get('id')} {t.get('product_id')} {t.get('direction')} · "
                f"{t.get('status')} · {_fmt_pct(t.get('pnl_pct'))}"
            )
    elif closed_n == 0:
        lines.extend(["", empty_closed_hint])

    return lines


def format_volume_book_report(
    report: dict[str, Any] | None, *, max_open_lines: int = 8
) -> str:
    """Telegram text for /performance (volume lane book)."""
    if not report or not report.get("available", True):
        return "Volume idea book is unavailable right now."

    lines = _format_book_body(
        report,
        max_open_lines=max_open_lines,
        empty_closed_hint="No closed trades yet — updates when price hits TP1 or SL.",
    )
    lines.extend(
        [
            "",
            "Volume lane book (all sized ideas). Your personal PnL: /me",
        ]
    )
    return "\n".join(lines)


def format_user_book_report(
    report: dict[str, Any] | None, *, max_open_lines: int = 8
) -> str:
    """Telegram text for /me (personal accepted-idea portfolio)."""
    if not report or not report.get("available", True):
        return "Your idea portfolio is unavailable right now."

    lines = _format_book_body(
        report,
        max_open_lines=max_open_lines,
        empty_closed_hint=(
            "No closed trades yet — Accept ideas to track personal PnL; "
            "updates when price hits TP1 or SL, or tap Close."
        ),
    )
    lines.extend(
        [
            "",
            "Personal book (ideas you Accepted). Tap Close below, or /performance for the mill.",
        ]
    )
    return "\n".join(lines)


CloseStatus = str  # closed | not_found | unavailable | no_spot


def get_open_user_trade(
    user_id: int, paper_id: int
) -> dict[str, Any] | None:
    """Return an open personal trade owned by ``user_id``, else None."""
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn:
            conn.executescript(_USER_PAPER_SCHEMA)
            row = conn.execute(
                """
                SELECT * FROM user_paper_trades
                WHERE id = ? AND user_id = ? AND status = 'open'
                """,
                (int(paper_id), int(user_id)),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        logger.exception(
            "get_open_user_trade failed user=%s paper=%s", user_id, paper_id
        )
        return None
    finally:
        conn.close()


def close_user_trade_at_spot(
    user_id: int,
    paper_id: int,
    spot: float,
) -> tuple[CloseStatus, dict[str, Any] | None]:
    """Manually close a personal open trade at spot. Ownership-checked."""
    conn = _connect()
    if conn is None:
        return "unavailable", None
    try:
        with conn:
            conn.executescript(_USER_PAPER_SCHEMA)
            row = conn.execute(
                """
                SELECT * FROM user_paper_trades
                WHERE id = ? AND user_id = ? AND status = 'open'
                """,
                (int(paper_id), int(user_id)),
            ).fetchone()
            if row is None:
                return "not_found", None
            trade = dict(row)
            entry = trade.get("entry")
            direction = str(trade.get("direction") or "")
            if entry is None or direction not in ("long", "short"):
                return "not_found", None
            entry_f = float(entry)
            spot_f = float(spot)
            if direction == "long":
                pnl_pct = (spot_f - entry_f) / entry_f * 100.0
            else:
                pnl_pct = (entry_f - spot_f) / entry_f * 100.0
            pnl_pct = round(pnl_pct, 4)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """
                UPDATE user_paper_trades
                SET status = 'manual', exit_price = ?, pnl_pct = ?, closed_at = ?
                WHERE id = ? AND user_id = ? AND status = 'open'
                """,
                (spot_f, pnl_pct, now, int(paper_id), int(user_id)),
            )
            closed = conn.execute(
                "SELECT * FROM user_paper_trades WHERE id = ?",
                (int(paper_id),),
            ).fetchone()
        return "closed", dict(closed) if closed else None
    except sqlite3.Error:
        logger.exception(
            "manual close failed user=%s paper=%s", user_id, paper_id
        )
        return "unavailable", None
    finally:
        conn.close()


def format_close_reply(
    status: CloseStatus, trade: dict[str, Any] | None = None
) -> str:
    if status == "closed" and trade:
        return (
            f"Closed #{trade.get('id')} {trade.get('product_id')} "
            f"{trade.get('direction')} @ {_fmt_px(trade.get('exit_price'))} · "
            f"{_fmt_pct(trade.get('pnl_pct'))}"
        )
    if status == "not_found":
        return "That position is not open (or isn’t yours)."
    if status == "no_spot":
        return "Couldn’t fetch a spot price to close — try again shortly."
    return "Couldn’t close that position right now — try again shortly."


_SOURCE_LABEL = {
    "news": "NEWS",
    "zmove": "SPIKE",
    "spike": "SPIKE",
    "funding": "FUNDING",
    "session": "SESSION",
    "cascade": "CASCADE",
}


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_take_profits(raw: Any) -> list[float]:
    if isinstance(raw, list):
        out: list[float] = []
        for item in raw:
            try:
                out.append(float(item))
            except (TypeError, ValueError):
                continue
        return out
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return _parse_take_profits(parsed)


def public_idea_card(
    row: dict[str, Any], *, my_decision: str | None = None
) -> dict[str, Any]:
    """User-facing idea payload — no filesystem paths, no internal keys."""
    source = str(row.get("source") or "")
    title = str(row.get("lay_title") or row.get("title") or "").strip()
    blurb = str(row.get("lay_blurb") or row.get("blurb") or "").strip()
    tps = row.get("take_profits")
    if tps is None:
        tps = _parse_take_profits(row.get("take_profits_json"))
    sent_at = row.get("sent_at")
    return {
        "id": int(row["id"]),
        "source": source,
        "source_label": _SOURCE_LABEL.get(source, source.upper() or "IDEA"),
        "product_id": str(row.get("product_id") or ""),
        "direction": str(row.get("direction") or ""),
        "title": title,
        "blurb": blurb,
        "stance_context": str(row.get("stance_context") or "").strip() or None,
        "confidence": row.get("confidence"),
        "entry": row.get("entry"),
        "stop_loss": row.get("stop_loss"),
        "take_profits": list(tps or []),
        "risk_reward": row.get("risk_reward"),
        "status": str(row.get("status") or ""),
        "created_at": row.get("created_at"),
        "telegram_sent": bool(sent_at),
        "my_decision": my_decision,
    }


def _decision_map(conn: sqlite3.Connection, user_id: int, idea_ids: list[int]) -> dict[int, str]:
    if not idea_ids:
        return {}
    placeholders = ",".join("?" * len(idea_ids))
    rows = conn.execute(
        f"""
        SELECT idea_id, decision FROM decisions
        WHERE user_id = ? AND idea_id IN ({placeholders})
        """,
        (int(user_id), *idea_ids),
    ).fetchall()
    return {int(r["idea_id"]): str(r["decision"]) for r in rows}


def idea_stream(
    *,
    limit: int = 40,
    after_id: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    """Shared mill stream: every sized long/short, not just Telegram broadcasts.

    ``after_id`` returns newer cards oldest-first for polling; otherwise the
    latest ``limit`` cards newest-first.
    """
    conn = _connect()
    if conn is None:
        return None
    cap = max(1, min(int(limit), 100))
    try:
        with conn:
            tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "ideas" not in tables:
                return {"available": True, "ideas": [], "latest_id": 0}

            if after_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM ideas
                    WHERE direction IN ('long', 'short')
                      AND entry IS NOT NULL
                      AND id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (int(after_id), cap),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM ideas
                    WHERE direction IN ('long', 'short')
                      AND entry IS NOT NULL
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (cap,),
                ).fetchall()

            raw = [dict(r) for r in rows]
            idea_ids = [int(r["id"]) for r in raw]
            mine: dict[int, str] = {}
            if user_id is not None and "decisions" in tables:
                mine = _decision_map(conn, user_id, idea_ids)

            latest_row = conn.execute("SELECT MAX(id) AS n FROM ideas").fetchone()
            latest_id = int(latest_row["n"] or 0) if latest_row else 0

        ideas = [
            public_idea_card(row, my_decision=mine.get(int(row["id"])))
            for row in raw
        ]
        return {
            "available": True,
            "ideas": ideas,
            "latest_id": latest_id,
        }
    except sqlite3.Error:
        logger.exception("idea stream read failed")
        return None
    finally:
        conn.close()


def idea_funnel(*, day: str | None = None) -> dict[str, Any] | None:
    """Mint vs Telegram vs Accept/Reject counts — the volume bottleneck meter."""
    conn = _connect()
    if conn is None:
        return None
    date = day or _utc_today()
    prefix = f"{date}%"
    try:
        with conn:
            tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            empty = {
                "available": True,
                "as_of_date": date,
                "minted": 0,
                "sized": 0,
                "telegram_sent": 0,
                "not_on_telegram": 0,
                "accepts": 0,
                "rejects": 0,
                "unique_users": 0,
            }
            if "ideas" not in tables:
                return empty

            minted = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM ideas WHERE created_at LIKE ?",
                    (prefix,),
                ).fetchone()["n"]
            )
            sized = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM ideas
                    WHERE created_at LIKE ?
                      AND direction IN ('long', 'short')
                      AND entry IS NOT NULL
                    """,
                    (prefix,),
                ).fetchone()["n"]
            )
            telegram_sent = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM ideas
                    WHERE created_at LIKE ? AND sent_at IS NOT NULL
                    """,
                    (prefix,),
                ).fetchone()["n"]
            )
            not_on_telegram = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM ideas
                    WHERE created_at LIKE ?
                      AND direction IN ('long', 'short')
                      AND entry IS NOT NULL
                      AND sent_at IS NULL
                    """,
                    (prefix,),
                ).fetchone()["n"]
            )
            accepts = rejects = unique_users = 0
            if "decisions" in tables:
                accepts = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM decisions
                        WHERE decided_at LIKE ? AND decision = 'accept'
                        """,
                        (prefix,),
                    ).fetchone()["n"]
                )
                rejects = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM decisions
                        WHERE decided_at LIKE ? AND decision = 'reject'
                        """,
                        (prefix,),
                    ).fetchone()["n"]
                )
                unique_users = int(
                    conn.execute(
                        """
                        SELECT COUNT(DISTINCT user_id) AS n FROM decisions
                        WHERE decided_at LIKE ?
                        """,
                        (prefix,),
                    ).fetchone()["n"]
                )
        return {
            "available": True,
            "as_of_date": date,
            "minted": minted,
            "sized": sized,
            "telegram_sent": telegram_sent,
            "not_on_telegram": not_on_telegram,
            "accepts": accepts,
            "rejects": rejects,
            "unique_users": unique_users,
        }
    except sqlite3.Error:
        logger.exception("idea funnel read failed")
        return None
    finally:
        conn.close()


def user_book_close_keyboard(
    report: dict[str, Any] | None, *, max_buttons: int = 8
):
    """Inline Close buttons for open personal trades. Returns None when empty."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not report:
        return None
    open_trades = list(report.get("open_trades") or [])[:max_buttons]
    if not open_trades:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for t in open_trades:
        pid = t.get("id")
        if pid is None:
            continue
        product = str(t.get("product_id") or "?").replace("-USD", "")
        direction = str(t.get("direction") or "")
        upnl = _fmt_pct(t.get("unrealized_pct"))
        rows.append(
            [
                InlineKeyboardButton(
                    f"Close #{pid} {product} {direction} ({upnl})",
                    callback_data=f"uportfolio:close:{int(pid)}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else None
