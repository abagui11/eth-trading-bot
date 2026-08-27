"""HQ vault: ICT ideas become sleeve allocations, not mill-style clips.

ICT propose → validate → critic is unchanged. This module is the capital
recipe sitting *after* a trade decision: admit / size / hold / kill.

Mill ideas never enter. User Accept on a vault card is a paper follow of an
already-admitted house allocation (follow-while-open, not a 15-minute offer).
Premium gating is still HQ_IDEAS_INTERNAL_ONLY — this file does not flip it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import bot_config
import config
import live_ledger
from models import Suggestion

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL UNIQUE,
    product_id TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    entry REAL,
    stop_loss REAL,
    take_profits_json TEXT,
    risk_reward REAL,
    notional_usd REAL NOT NULL,
    qty REAL,
    nav_usd REAL NOT NULL,
    deploy_pct REAL NOT NULL,
    admitted INTEGER NOT NULL,
    skip_reason TEXT,
    status TEXT NOT NULL,
    title TEXT,
    blurb TEXT,
    order_block_ref TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_vault_status ON vault_allocations(status);

CREATE TABLE IF NOT EXISTS vault_follows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    allocation_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE (allocation_id, user_id),
    FOREIGN KEY (allocation_id) REFERENCES vault_allocations(id)
);
"""


@dataclass(frozen=True)
class VaultPolicy:
    nav_usd: float
    deploy_pct: float
    max_open: int
    max_leverage: float
    max_per_product: int
    daily_loss_limit_usd: float
    scale_ins_allowed: bool
    allowed_products: tuple[str, ...]
    hold_horizon: str
    premium_gated: bool
    qty_floors: dict[str, float]


def policy() -> VaultPolicy:
    """House HQ sleeve recipe. Numbers stay in bot_config so live + vault agree."""
    return VaultPolicy(
        nav_usd=float(bot_config.LIVE_HQ_EQUITY_USD),
        deploy_pct=float(bot_config.LIVE_TRADE_DEPLOY_PCT),
        max_open=int(bot_config.LIVE_MAX_OPEN_HQ),
        max_leverage=float(bot_config.LIVE_MAX_LEVERAGE),
        max_per_product=1,
        daily_loss_limit_usd=float(bot_config.LIVE_DAILY_LOSS_LIMIT_USD),
        scale_ins_allowed=bool(bot_config.LIVE_SCALE_IN_ENABLED),
        allowed_products=tuple(bot_config.TRADED_PRODUCTS),
        hold_horizon="swing",
        premium_gated=bool(bot_config.HQ_IDEAS_INTERNAL_ONLY),
        qty_floors=dict(bot_config.LIVE_PRODUCT_QTY_FLOORS),
    )


def policy_public(p: VaultPolicy | None = None) -> dict[str, Any]:
    p = p or policy()
    return {
        "nav_usd": p.nav_usd,
        "deploy_pct": p.deploy_pct,
        "notional_per_name_usd": round(p.nav_usd * p.deploy_pct, 2),
        "max_open": p.max_open,
        "max_leverage": p.max_leverage,
        "max_per_product": p.max_per_product,
        "daily_loss_limit_usd": p.daily_loss_limit_usd,
        "scale_ins_allowed": p.scale_ins_allowed,
        "allowed_products": list(p.allowed_products),
        "hold_horizon": p.hold_horizon,
        "premium_gated": p.premium_gated,
        "mill_excluded": True,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    live_ledger.init_db()
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _side(action: str) -> str:
    if action in ("deriv_buy", "spot_buy"):
        return "long"
    return "short"


def _is_scale_in(suggestion: Suggestion) -> bool:
    tranche = (suggestion.entry_tranche or "").strip()
    if tranche == str(bot_config.ADD_FIB_LEVEL):
        return True
    text = suggestion.rationale or ""
    return "Scale-in" in text or "adds 25% notional" in text.lower()


def _halt_reason() -> str | None:
    reason = live_ledger.get_meta("live_halt")
    if not reason:
        return None
    halt_date = live_ledger.get_meta("live_halt_date")
    if halt_date and halt_date < _today() and str(reason).startswith("daily_loss"):
        return None
    return str(reason)


def _realized_pnl_today() -> float:
    total = 0.0
    for trade in live_ledger.get_closed_trades(limit=200, source="hq"):
        if (trade.get("closed_at") or "").startswith(_today()):
            total += float(trade.get("pnl_usd") or 0.0)
    return total


def _live_open_cycles() -> set[str]:
    return {
        str(t["cycle_id"])
        for t in live_ledger.get_open_trades(source="hq")
        if t.get("cycle_id")
    }


def _paper_open_cycles() -> set[str]:
    try:
        import paper
    except Exception:
        return set()
    try:
        rows = paper.get_open_positions()
    except Exception:
        logger.exception("Vault could not read house paper opens")
        return set()
    return {str(p["open_cycle_id"]) for p in rows if p.get("open_cycle_id")}


def _sync_closes(conn: sqlite3.Connection) -> None:
    """Close vault rows whose house paper and live HQ fills are both gone."""
    still_open = _paper_open_cycles() | _live_open_cycles()
    open_rows = conn.execute(
        "SELECT id, cycle_id FROM vault_allocations WHERE status = 'open' AND admitted = 1"
    ).fetchall()
    now = _now_iso()
    for row in open_rows:
        cycle_id = str(row["cycle_id"] or "")
        if cycle_id and cycle_id not in still_open:
            conn.execute(
                """
                UPDATE vault_allocations
                SET status = 'closed', closed_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (now, int(row["id"])),
            )


def open_allocations() -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        _sync_closes(conn)
        conn.commit()
        rows = conn.execute(
            """
            SELECT * FROM vault_allocations
            WHERE status = 'open' AND admitted = 1
            ORDER BY id DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _exposure_rows() -> list[dict[str, Any]]:
    """Open vault names plus live HQ fills not yet booked into the vault."""
    rows = open_allocations()
    seen = {str(r.get("cycle_id") or "") for r in rows}
    seen.discard("")
    for trade in live_ledger.get_open_trades(source="hq"):
        cycle_id = str(trade.get("cycle_id") or "")
        if cycle_id and cycle_id in seen:
            continue
        qty = float(trade.get("qty") or 0)
        entry = float(trade.get("entry") or 0)
        rows.append(
            {
                "cycle_id": cycle_id,
                "product_id": trade.get("product_id"),
                "notional_usd": qty * entry,
            }
        )
        if cycle_id:
            seen.add(cycle_id)
    return rows


def propose(
    suggestion: Suggestion,
    *,
    spot: float | None = None,
    open_rows: list[dict[str, Any]] | None = None,
    p: VaultPolicy | None = None,
) -> dict[str, Any]:
    """Pure admit/size decision. Does not write. Mill / no_trade never admit."""
    p = p or policy()
    opens = list(open_rows if open_rows is not None else _exposure_rows())
    action = str(suggestion.action or "")
    product_id = str(suggestion.product_id or "")
    entry = suggestion.entry if suggestion.entry is not None else spot
    stop = suggestion.stop_loss

    def skip(reason: str) -> dict[str, Any]:
        return {
            "admitted": False,
            "skip_reason": reason,
            "notional_usd": 0.0,
            "qty": None,
            "risk_usd": None,
            "heat_after_pct": _heat_pct(opens, p),
            "nav_usd": p.nav_usd,
            "deploy_pct": p.deploy_pct,
        }

    if action not in ("deriv_buy", "deriv_sell", "spot_buy", "spot_sell"):
        return skip("no_trade")
    if product_id not in p.allowed_products:
        return skip(f"product_not_allowed:{product_id}")
    if stop is None or entry is None or float(entry) <= 0:
        return skip("missing_levels")
    if _is_scale_in(suggestion) and not p.scale_ins_allowed:
        return skip("scale_in")

    halt = _halt_reason()
    if halt:
        return skip(f"halted:{halt}")
    pnl = _realized_pnl_today()
    if pnl <= -p.daily_loss_limit_usd:
        return skip(f"daily_loss:{pnl:.2f}")

    if len(opens) >= p.max_open:
        return skip("sleeve_full")
    same = [r for r in opens if str(r.get("product_id")) == product_id]
    if len(same) >= p.max_per_product:
        return skip(f"product_open:{product_id}")

    notional = p.nav_usd * p.deploy_pct
    open_notional = sum(float(r.get("notional_usd") or 0) for r in opens)
    if open_notional + notional > p.nav_usd * p.max_leverage + 1e-9:
        return skip("heat_cap")

    qty = notional / float(entry)
    floor = p.qty_floors.get(product_id)
    if floor is not None and qty < floor:
        return skip(f"qty_floor:{qty:.6f}<{floor}")

    risk_per_unit = abs(float(entry) - float(stop))
    risk_usd = risk_per_unit * qty
    heat_after = (open_notional + notional) / p.nav_usd if p.nav_usd else 0.0
    return {
        "admitted": True,
        "skip_reason": None,
        "notional_usd": round(notional, 2),
        "qty": round(qty, 6),
        "risk_usd": round(risk_usd, 2),
        "heat_after_pct": round(heat_after, 4),
        "nav_usd": p.nav_usd,
        "deploy_pct": p.deploy_pct,
    }


def _heat_pct(opens: list[dict[str, Any]], p: VaultPolicy) -> float:
    if p.nav_usd <= 0:
        return 0.0
    return round(sum(float(r.get("notional_usd") or 0) for r in opens) / p.nav_usd, 4)


def get_by_cycle(cycle_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM vault_allocations WHERE cycle_id = ?",
            (str(cycle_id),),
        ).fetchone()
    return _row_public(dict(row)) if row else None


def take(
    suggestion: Suggestion,
    *,
    cycle_id: str,
    spot: float | None = None,
    title: str | None = None,
    blurb: str | None = None,
) -> dict[str, Any]:
    """Admit or skip once per cycle. Idempotent."""
    init_db()
    existing = get_by_cycle(cycle_id)
    if existing is not None:
        return existing

    decision = propose(suggestion, spot=spot)
    action = str(suggestion.action or "no_trade")
    side = _side(action) if decision["admitted"] else _side(action)
    tps = list(suggestion.take_profits or [])
    status = "open" if decision["admitted"] else "skipped"
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO vault_allocations (
                cycle_id, product_id, side, action, entry, stop_loss,
                take_profits_json, risk_reward, notional_usd, qty, nav_usd,
                deploy_pct, admitted, skip_reason, status, title, blurb,
                order_block_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(cycle_id),
                str(suggestion.product_id or ""),
                side,
                action,
                suggestion.entry,
                suggestion.stop_loss,
                json.dumps(tps),
                suggestion.risk_reward,
                float(decision["notional_usd"]),
                decision["qty"],
                float(decision["nav_usd"]),
                float(decision["deploy_pct"]),
                1 if decision["admitted"] else 0,
                decision["skip_reason"],
                status,
                title,
                blurb,
                suggestion.order_block_ref,
                _now_iso(),
            ),
        )
        conn.commit()
        row_id = int(cur.lastrowid or 0)
    row = get_by_cycle(cycle_id)
    if row is None:
        return {**decision, "id": row_id, "cycle_id": cycle_id, "status": status}
    return row


def snapshot() -> dict[str, Any]:
    p = policy()
    opens = open_allocations()
    halt = _halt_reason()
    pnl = _realized_pnl_today()
    exposure = _exposure_rows()
    return {
        "policy": policy_public(p),
        "nav_usd": p.nav_usd,
        "open_count": len(opens),
        "open": [_card(r) for r in opens],
        "heat_pct": _heat_pct(exposure, p),
        "realized_pnl_today": round(pnl, 2),
        "halted": bool(halt),
        "halt_reason": halt,
        "daily_loss_limit_usd": p.daily_loss_limit_usd,
        "premium_gated": p.premium_gated,
    }


def _parse_tps(raw: Any) -> list[float]:
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
        return _parse_tps(json.loads(str(raw)))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def _row_public(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["take_profits"] = _parse_tps(row.get("take_profits_json"))
    row["admitted"] = bool(row.get("admitted"))
    return row


def _card(row: dict[str, Any], *, my_decision: str | None = None) -> dict[str, Any]:
    data = _row_public(row)
    p = policy()
    return {
        "id": int(data["id"]),
        "rail": "hq",
        "cycle_id": data.get("cycle_id"),
        "product_id": data.get("product_id"),
        "direction": data.get("side"),
        "title": data.get("title") or f"High Quality · {data.get('product_id')}",
        "blurb": data.get("blurb") or "",
        "entry": data.get("entry"),
        "stop_loss": data.get("stop_loss"),
        "take_profits": data.get("take_profits") or [],
        "risk_reward": data.get("risk_reward"),
        "notional_usd": data.get("notional_usd"),
        "nav_usd": data.get("nav_usd"),
        "deploy_pct": data.get("deploy_pct"),
        "hold_horizon": p.hold_horizon,
        "status": data.get("status"),
        "followable": data.get("status") == "open" and bool(data.get("admitted")),
        "created_at": data.get("created_at"),
        "my_decision": my_decision,
        "premium_gated": p.premium_gated,
    }


def stream(
    *,
    limit: int = 20,
    after_id: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Admitted HQ allocations, newest first (or newer than after_id)."""
    init_db()
    cap = max(1, min(int(limit), 50))
    with _connect() as conn:
        if after_id is not None:
            rows = conn.execute(
                """
                SELECT * FROM vault_allocations
                WHERE admitted = 1 AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(after_id), cap),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM vault_allocations
                WHERE admitted = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (cap,),
            ).fetchall()
        latest_row = conn.execute(
            "SELECT MAX(id) AS n FROM vault_allocations WHERE admitted = 1"
        ).fetchone()
        latest_id = int(latest_row["n"] or 0) if latest_row else 0
        idea_ids = [int(r["id"]) for r in rows]
        mine: dict[int, str] = {}
        if user_id is not None and idea_ids:
            placeholders = ",".join("?" * len(idea_ids))
            decisions = conn.execute(
                f"""
                SELECT allocation_id, decision FROM vault_follows
                WHERE user_id = ? AND allocation_id IN ({placeholders})
                """,
                (int(user_id), *idea_ids),
            ).fetchall()
            mine = {int(d["allocation_id"]): str(d["decision"]) for d in decisions}
    cards = [_card(dict(r), my_decision=mine.get(int(r["id"]))) for r in rows]
    return {"available": True, "ideas": cards, "latest_id": latest_id}


def record_follow(allocation_id: int, user_id: int, decision: str) -> str:
    """accept | reject. Accept is follow-while-open. Returns recorded|duplicate|..."""
    if decision not in ("accept", "reject"):
        raise ValueError(f"invalid decision: {decision}")
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM vault_allocations WHERE id = ?",
            (int(allocation_id),),
        ).fetchone()
        if row is None or not int(row["admitted"]):
            return "unknown_idea"
        if decision == "accept" and str(row["status"]) != "open":
            return "closed"
        try:
            conn.execute(
                """
                INSERT INTO vault_follows (allocation_id, user_id, decision, decided_at)
                VALUES (?, ?, ?, ?)
                """,
                (int(allocation_id), int(user_id), decision, _now_iso()),
            )
        except sqlite3.IntegrityError:
            return "duplicate"
        conn.commit()
    return "recorded"


def follow(
    allocation_id: int,
    user_id: int,
    decision: str,
    *,
    spots: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Record follow + on Accept try to open a personal paper clip (demo book)."""
    status = record_follow(allocation_id, user_id, decision)
    paper: dict[str, Any] | None = None
    if status == "recorded" and decision == "accept":
        paper = _open_user_paper(allocation_id, user_id, spots=spots)
    message = {
        "recorded": (
            "Following this vault allocation."
            if decision == "accept"
            else "Rejected this vault allocation."
        ),
        "duplicate": "You already decided on this vault allocation.",
        "unknown_idea": "Vault allocation not found.",
        "closed": "This vault allocation is no longer open to follow.",
    }.get(status, status)
    if paper and paper.get("ok"):
        message = "Following this vault allocation — sized in your paper book (/me)."
    elif paper and paper.get("reason") == "no_account":
        message = (
            "Following this vault allocation. Open a demo account in Telegram "
            "to size it in My book."
        )
    return {
        "ok": status == "recorded",
        "status": status,
        "decision": decision,
        "allocation_id": int(allocation_id),
        "paper": paper,
        "message": message,
    }


def _open_user_paper(
    allocation_id: int,
    user_id: int,
    *,
    spots: dict[str, float] | None = None,
) -> dict[str, Any]:
    import user_books

    row = None
    with _connect() as conn:
        fetched = conn.execute(
            "SELECT * FROM vault_allocations WHERE id = ?",
            (int(allocation_id),),
        ).fetchone()
        if fetched is not None:
            row = dict(fetched)
    if row is None:
        return {"ok": False, "reason": "unknown_idea"}
    if user_books.get_account(user_id) is None:
        return {"ok": False, "reason": "no_account"}
    tps = _parse_tps(row.get("take_profits_json"))
    suggestion = Suggestion(
        action=str(row.get("action") or "deriv_buy"),
        size=float(row.get("notional_usd") or 0),
        entry=row.get("entry"),
        stop_loss=row.get("stop_loss"),
        take_profits=tps,
        risk_reward=row.get("risk_reward"),
        rationale="vault follow",
        product_id=str(row.get("product_id") or "ETH-USD"),
    )
    return user_books.open_from_vault(
        user_id,
        allocation_id=int(allocation_id),
        cycle_id=str(row.get("cycle_id") or ""),
        suggestion=suggestion,
        spots=spots,
    )
