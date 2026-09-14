"""Paper books for the Eva HQ variant experiment — day / swing / control mirror.

Deliberately a **separate module with its own tables**, not a `variant` column
on ``paper_positions``. Two reasons, both structural:

* ``paper_state`` is ``CHECK (id = 1)`` — a single-row table with one cash
  balance. It cannot represent four books.
* ``paper._fetch_open_positions`` selects every open row unscoped, and feeds
  netting, the dashboard, Telegram and the live-execution checks. Re-scoping
  all of that inside the module that runs the control book is exactly what
  "keep control untouched" forbids.

So ``paper.py`` is not modified at all. Control *is* ``paper.py``; the variants
live here and are only ever written by their own code paths.

Exits resolve on the **M5 high/low path**, never on poll-time spot — the same
correctness standard ``paper.py`` adopted after three recorded winners were
found to have traded through their stop between polls. Ambiguous bars resolve
stop-first.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import bot_config
import config

logger = logging.getLogger(__name__)

# The four books. ``control`` is a read-only alias for paper.py's house book and
# is never written here; it exists so the dashboard can list all four uniformly.
CONTROL = "control"
SWING_MECH = "eva_swing_mech"
SWING_LLM = "eva_swing_llm"
DAY = "eva_day"

VARIANTS: tuple[str, ...] = (CONTROL, SWING_MECH, SWING_LLM, DAY)
WRITABLE: tuple[str, ...] = (SWING_MECH, SWING_LLM, DAY)

# Every book risks the same dollars per trade, so a wider stop buys a smaller
# position. Without this the swing arms would out-earn control by betting more
# rather than by being right, and the R comparison would be meaningless.
VARIANT_RISK_USD: float = float(getattr(bot_config, "VARIANT_RISK_USD", 10.0))

# Eva's action vocabulary is `spot_buy` / `spot_sell` / `deriv_buy` /
# `deriv_sell` (see `analyze.VALID_ACTIONS`), *not* "long"/"short". Mapping only
# the latter silently returns None for every real suggestion, which makes the
# mirrors no-ops that log nothing — the failure mode is an empty book, not an
# error. Kept here so all three variant modules share one definition.
#
# Unlike `live_pending.side_of`, anything unrecognised maps to None rather than
# defaulting to "short": a variant must decline an action it does not
# understand, never guess a direction.
_LONG_ACTIONS = frozenset({"spot_buy", "deriv_buy", "long", "buy", "bullish"})
_SHORT_ACTIONS = frozenset({"spot_sell", "deriv_sell", "short", "sell", "bearish"})


def side_of_action(action: str | None) -> str | None:
    """"long" / "short" for a tradeable action, else None (incl. no_trade)."""
    a = str(action or "").strip().lower()
    if a in _LONG_ACTIONS:
        return "long"
    if a in _SHORT_ACTIONS:
        return "short"
    return None

LABELS = {
    CONTROL: "Control (live Eva HQ)",
    SWING_MECH: "Swing — mechanical re-bracket",
    SWING_LLM: "Swing — H12/D1 mandate",
    DAY: "Day — fast ICT",
}

BLURBS = {
    CONTROL: "The shipped bot, untouched. LLM vision every 30 min, ~1% stop, "
             "three-rung ladder. The baseline every variant is measured against.",
    SWING_MECH: "Control's exact entries with the stop moved beyond the H4 "
                "structure that invalidates the thesis and targets scaled out. "
                "No LLM, no prompt change — isolates exit geometry.",
    SWING_LLM: "Same brain, swing mandate: sees H12/D1 and is told to place "
               "stops at structural invalidation and targets at HTF objectives.",
    DAY: "Fast ICT. Deterministic M1/M5 triggers gated on the last vision "
         "stance, plus day-bracketed mirrors of vision entries. Hard 4h close.",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS variant_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant TEXT NOT NULL,
    entry_source TEXT NOT NULL,        -- vision_mirror | vision_rebracket | m1_trigger
    cycle_id TEXT,
    product_id TEXT NOT NULL,
    side TEXT NOT NULL,                -- long | short
    opened_at TEXT NOT NULL,
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,           -- opening stop; never overwritten
    take_profits TEXT NOT NULL,        -- JSON list, nearest first
    qty REAL NOT NULL,
    risk_usd REAL NOT NULL,
    max_hold_hours REAL,               -- NULL = no time exit
    trigger_name TEXT,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open | closed
    closed_at TEXT,
    exit_price REAL,
    close_reason TEXT,                 -- stop | target | time_exit
    tps_hit INTEGER NOT NULL DEFAULT 0,
    realized_r REAL,
    realized_pnl_usd REAL,
    mfe_r REAL,
    mae_r REAL,
    path_checked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_variant_positions_open
    ON variant_positions (variant, status);

CREATE TABLE IF NOT EXISTS variant_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant TEXT NOT NULL,
    position_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,               -- open | target | stop | time_exit
    price REAL,
    qty REAL,
    r REAL,
    pnl_usd REAL
);

CREATE TABLE IF NOT EXISTS variant_skips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant TEXT NOT NULL,
    ts TEXT NOT NULL,
    product_id TEXT,
    side TEXT,
    trigger_name TEXT,
    reason TEXT NOT NULL
);
"""


@contextmanager
def _connect():
    """Commit-on-success and **close** handle.

    ``with sqlite3.connect(...)`` commits but does not close — a long-lived
    scheduler process calling this every 120s would accumulate handles for as
    long as it runs. The explicit close is why this is a contextmanager rather
    than the bare-connect helper used elsewhere in the codebase.
    """
    conn = sqlite3.connect(config.LEDGER_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------- opening

def record_skip(
    variant: str,
    *,
    reason: str,
    product_id: str | None = None,
    side: str | None = None,
    trigger_name: str | None = None,
) -> None:
    """Log a trigger that fired but did not open, so the denominator is honest.

    Without this the cooldown silently hides how often the day bot wanted in,
    and the fire rate looks like the position rate.
    """
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO variant_skips (variant, ts, product_id, side, "
            "trigger_name, reason) VALUES (?,?,?,?,?,?)",
            (variant, _now_iso(), product_id, side, trigger_name, reason),
        )


def has_open(variant: str, product_id: str, side: str | None = None) -> bool:
    """Cooldown predicate: one open position per product per side."""
    init_db()
    sql = ("SELECT 1 FROM variant_positions WHERE variant = ? AND status = 'open' "
           "AND product_id = ?")
    args: list[Any] = [variant, product_id]
    if side is not None:
        sql += " AND side = ?"
        args.append(side)
    with _connect() as conn:
        return conn.execute(sql + " LIMIT 1", args).fetchone() is not None


def open_position(
    variant: str,
    *,
    product_id: str,
    side: str,
    entry: float,
    stop_loss: float,
    take_profits: Sequence[float],
    entry_source: str,
    cycle_id: str | None = None,
    trigger_name: str | None = None,
    rationale: str | None = None,
    max_hold_hours: float | None = None,
    risk_usd: float | None = None,
    opened_at: str | None = None,
) -> int | None:
    """Open a variant paper position. Returns the row id, or None if rejected.

    Size is derived from a fixed risk budget so every book risks the same
    dollars per trade regardless of its stop width. That is what makes the
    books comparable in R — a wider stop must buy a smaller position, or the
    swing arms would simply be betting more.
    """
    if variant not in WRITABLE:
        raise ValueError(f"{variant} is not a writable variant book")
    if side not in ("long", "short"):
        return None
    if entry <= 0 or stop_loss <= 0:
        return None
    risk_per_unit = abs(entry - stop_loss)
    if risk_per_unit <= 0:
        return None

    tps = [float(t) for t in take_profits if t and float(t) > 0]
    if not tps:
        return None
    # Nearest target first, in the direction of the trade.
    tps.sort(reverse=(side == "short"))

    budget = float(risk_usd if risk_usd is not None else VARIANT_RISK_USD)
    qty = budget / risk_per_unit

    init_db()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO variant_positions
                (variant, entry_source, cycle_id, product_id, side, opened_at,
                 entry, stop_loss, take_profits, qty, risk_usd, max_hold_hours,
                 trigger_name, rationale, path_checked_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (variant, entry_source, cycle_id, product_id, side,
             opened_at or _now_iso(), entry, stop_loss, json.dumps(tps), qty,
             budget, max_hold_hours, trigger_name, rationale,
             opened_at or _now_iso()),
        )
        pid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO variant_trades (variant, position_id, ts, event, "
            "price, qty) VALUES (?,?,?,?,?,?)",
            (variant, pid, opened_at or _now_iso(), "open", entry, qty),
        )
    logger.info(
        "variant %s opened %s %s @ %.2f stop %.2f tps %s (%s)",
        variant, side, product_id, entry, stop_loss, tps, entry_source,
    )
    return pid


# --------------------------------------------------------------- resolution

@dataclass
class _Bar:
    ts: int
    high: float
    low: float
    close: float


def _m5_path(product_id: str, since: str | None) -> list[_Bar]:
    """M5 bars since the last walk, oldest first. Empty on any failure."""
    start_dt = _parse(since)
    if start_dt is None:
        return []
    start = int(start_dt.timestamp())
    end = int(datetime.now(timezone.utc).timestamp())
    if start >= end:
        return []
    try:
        import research

        raw = research.fetch_coinbase_candles_range(
            "FIVE_MINUTE", start, end, product_id=product_id
        )
    except Exception:
        logger.warning(
            "variants: M5 path unavailable for %s since %s; position left open",
            product_id, since, exc_info=True,
        )
        return []
    out: list[_Bar] = []
    for c in raw:
        ts = _parse(str(c.get("ts")))
        if ts is None:
            continue
        out.append(_Bar(int(ts.timestamp()), float(c["high"]),
                        float(c["low"]), float(c["close"])))
    out.sort(key=lambda b: b.ts)
    return out


def _r_of(side: str, entry: float, stop: float, price: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    sign = 1.0 if side == "long" else -1.0
    return sign * (price - entry) / risk


def _resolve_one(pos: dict, bars: list[_Bar]) -> dict:
    """Walk a position's bars. Returns a close dict, or None if still open.

    Ladder semantics match Eva's: equal fractions at each rung, the stop moving
    to breakeven after the first target and to the previous rung thereafter.
    A bar that touches both the stop and a target resolves stop-first.
    """
    side = str(pos["side"])
    entry = float(pos["entry"])
    stop0 = float(pos["stop_loss"])
    tps: list[float] = json.loads(pos["take_profits"])
    n_rungs = len(tps)
    filled = int(pos["tps_hit"] or 0)
    risk = abs(entry - stop0)
    sign = 1.0 if side == "long" else -1.0

    banked = 0.0
    for i in range(filled):
        banked += _r_of(side, entry, stop0, tps[i]) / n_rungs

    # Stop in R terms: -1 until TP1, then breakeven, then the prior rung.
    def stop_r_for(f: int) -> float:
        if f <= 0:
            return -1.0
        if f == 1:
            return 0.0
        return _r_of(side, entry, stop0, tps[f - 2])

    mfe = float(pos["mfe_r"] or 0.0)
    mae = float(pos["mae_r"] or 0.0)
    deadline = None
    if pos["max_hold_hours"]:
        opened = _parse(pos["opened_at"])
        if opened:
            deadline = opened + timedelta(hours=float(pos["max_hold_hours"]))

    for bar in bars:
        fav = _r_of(side, entry, stop0, bar.high if side == "long" else bar.low)
        adv = _r_of(side, entry, stop0, bar.low if side == "long" else bar.high)
        mfe = max(mfe, fav)
        mae = min(mae, adv)

        stop_px = entry + sign * stop_r_for(filled) * risk
        stop_touched = bar.low <= stop_px if side == "long" else bar.high >= stop_px
        if stop_touched:
            rem = (n_rungs - filled) / n_rungs
            realized = banked + rem * stop_r_for(filled)
            return {"exit_price": stop_px, "reason": "stop", "r": realized,
                    "ts": bar.ts, "tps_hit": filled, "mfe": mfe, "mae": mae}

        # Targets, nearest first; stop-first already handled above.
        while filled < n_rungs:
            tp = tps[filled]
            hit = bar.high >= tp if side == "long" else bar.low <= tp
            if not hit:
                break
            banked += _r_of(side, entry, stop0, tp) / n_rungs
            filled += 1
        if filled >= n_rungs:
            return {"exit_price": tps[-1], "reason": "target", "r": banked,
                    "ts": bar.ts, "tps_hit": filled, "mfe": mfe, "mae": mae}

        if deadline is not None and datetime.fromtimestamp(
            bar.ts, tz=timezone.utc
        ) >= deadline:
            rem = (n_rungs - filled) / n_rungs
            realized = banked + rem * _r_of(side, entry, stop0, bar.close)
            return {"exit_price": bar.close, "reason": "time_exit",
                    "r": realized, "ts": bar.ts, "tps_hit": filled,
                    "mfe": mfe, "mae": mae}

    return {"_progress": True, "tps_hit": filled, "mfe": mfe, "mae": mae,
            "last_ts": bars[-1].ts if bars else None}


def mark_to_market(now: datetime | None = None) -> int:
    """Resolve every open variant position on its M5 path. Returns closes."""
    init_db()
    now = now or datetime.now(timezone.utc)
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM variant_positions WHERE status = 'open' ORDER BY id"
        )]
    closed = 0
    for pos in rows:
        bars = _m5_path(str(pos["product_id"]), str(pos["path_checked_at"]))
        if not bars:
            continue
        res = _resolve_one(pos, bars)
        with _connect() as conn:
            if res.get("_progress"):
                conn.execute(
                    "UPDATE variant_positions SET tps_hit = ?, mfe_r = ?, "
                    "mae_r = ?, path_checked_at = ? WHERE id = ?",
                    (res["tps_hit"], res["mfe"], res["mae"],
                     datetime.fromtimestamp(res["last_ts"], tz=timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ") if res["last_ts"]
                     else pos["path_checked_at"], pos["id"]),
                )
                continue
            ts_iso = datetime.fromtimestamp(
                res["ts"], tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            pnl = float(res["r"]) * float(pos["risk_usd"])
            conn.execute(
                "UPDATE variant_positions SET status='closed', closed_at=?, "
                "exit_price=?, close_reason=?, tps_hit=?, realized_r=?, "
                "realized_pnl_usd=?, mfe_r=?, mae_r=?, path_checked_at=? "
                "WHERE id = ?",
                (ts_iso, res["exit_price"], res["reason"], res["tps_hit"],
                 res["r"], pnl, res["mfe"], res["mae"], ts_iso, pos["id"]),
            )
            conn.execute(
                "INSERT INTO variant_trades (variant, position_id, ts, event, "
                "price, qty, r, pnl_usd) VALUES (?,?,?,?,?,?,?,?)",
                (pos["variant"], pos["id"], ts_iso, res["reason"],
                 res["exit_price"], pos["qty"], res["r"], pnl),
            )
        closed += 1
        logger.info(
            "variant %s closed #%s %s %.3fR (%s)",
            pos["variant"], pos["id"], pos["product_id"], res["r"], res["reason"],
        )
    return closed


# --------------------------------------------------------------- reporting

def open_positions(variant: str | None = None) -> list[dict]:
    init_db()
    sql = "SELECT * FROM variant_positions WHERE status = 'open'"
    args: list[Any] = []
    if variant:
        sql += " AND variant = ?"
        args.append(variant)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY opened_at", args)]


def closed_positions(variant: str | None = None, limit: int = 200) -> list[dict]:
    init_db()
    sql = "SELECT * FROM variant_positions WHERE status = 'closed'"
    args: list[Any] = []
    if variant:
        sql += " AND variant = ?"
        args.append(variant)
    sql += " ORDER BY closed_at DESC LIMIT ?"
    args.append(int(limit))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def skip_counts(variant: str, since: str | None = None) -> int:
    init_db()
    sql = "SELECT COUNT(*) FROM variant_skips WHERE variant = ?"
    args: list[Any] = [variant]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    with _connect() as conn:
        return int(conn.execute(sql, args).fetchone()[0])


def summary(variant: str, since: str | None = None) -> dict:
    """Per-book stats for the dashboard. R-based so books compare directly."""
    rows = [r for r in closed_positions(variant, limit=10_000)
            if not since or str(r["closed_at"] or "") >= since]
    rs = [float(r["realized_r"]) for r in rows if r["realized_r"] is not None]
    wins = [r for r in rs if r > 0]
    holds = []
    for r in rows:
        a, b = _parse(r["opened_at"]), _parse(r["closed_at"])
        if a and b:
            holds.append((b - a).total_seconds() / 3600)
    # The mechanism metric the pre-registration turns on: theses that were
    # correct but killed by the stop.
    stopped = [r for r in rows if str(r["close_reason"]) == "stop"]
    stopped_then_paid = [
        r for r in stopped
        if r["mfe_r"] is not None and float(r["mfe_r"]) > 0.0
    ]
    return {
        "variant": variant,
        "label": LABELS.get(variant, variant),
        "blurb": BLURBS.get(variant, ""),
        "n_closed": len(rows),
        "n_open": len(open_positions(variant)),
        "win_rate": (len(wins) / len(rs)) if rs else None,
        "mean_r": (sum(rs) / len(rs)) if rs else None,
        "sum_r": sum(rs) if rs else 0.0,
        "pnl_usd": sum(float(r["realized_pnl_usd"] or 0) for r in rows),
        "median_hold_h": (sorted(holds)[len(holds) // 2] if holds else None),
        "stopped_n": len(stopped),
        "stopped_then_paid": len(stopped_then_paid),
        "skips": skip_counts(variant, since),
    }
