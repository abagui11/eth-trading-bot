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
        elif status in ("hit_tp", "hit_sl"):
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

    lines = [
        report.get("label") or "Idea book",
        "",
        f"Open {open_n} · Closed {closed_n} (TP {tp_n} / SL {sl_n})",
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
            "updates when price hits TP1 or SL."
        ),
    )
    lines.extend(
        [
            "",
            "Personal book (ideas you Accepted). Overall mill: /performance",
        ]
    )
    return "\n".join(lines)
