"""Live plans waiting for their entry.

Eva's entries are pullback limits into an M5 order block, so when a plan is
minted the market is usually not there yet. Sending a market order anyway buys
a different trade from the one she analysed: the stop stays where the chart put
it, so every point of chase widens the risk and shortens the run to the first
target at the same time.

Measured over the 15 HQ plans of the current epoch
(``analysis/run_fill_study.py`` in the trade_ideas repo, engine calibrated
against HANDOFF.md's +0.346R baseline): plans whose entry sat more than about
0.2% away were worth **+0.317R at the price Eva asked for and -0.352R at the
price live actually paid**. Price never came back to those levels, so the good
number was never available — the only way to avoid the bad one was to not take
the trade. Buying the mark instead turned the book from +0.375R into -0.011R.

The effect is not statistically established (p = 0.269 on 15 trades, and it
would need roughly 50 per arm to resolve), so this ships on the structure
rather than on the number: a plan whose entry has not traded is a plan whose
premise has not been confirmed.

Deliberately *not* copied from ``paper.py``: paper resolves a touch by walking
M5 bar extremes since its last check, which lets it fill retroactively at a
wick from twenty minutes ago. A market order cannot do that — it fills at the
price now — so this tests the current mark only. A wick between polls is a
fill genuinely missed, not one to chase, and the two books will diverge
slightly for that reason. That divergence is correct.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import bot_config
import config
from models import Suggestion

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    product_id TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT NOT NULL,                           -- 'long' | 'short'
    entry REAL NOT NULL,                          -- the limit price we wait for
    stop_loss REAL NOT NULL,
    take_profits_json TEXT,
    risk_reward REAL,
    size REAL,
    order_block_ref TEXT,
    entry_tranche TEXT,
    rationale TEXT,
    created_at TEXT NOT NULL,
    checked_at TEXT
);
-- One waiting plan per product: a fresh read of the same chart replaces the
-- old one rather than resting beside it.
CREATE UNIQUE INDEX IF NOT EXISTS live_pending_product
    ON live_pending (product_id);
"""

TRADE_ACTIONS = ("spot_buy", "spot_sell", "deriv_buy", "deriv_sell")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def init_db() -> None:
    with _connect():
        pass


def side_of(action: str) -> str:
    return "long" if action in ("deriv_buy", "spot_buy") else "short"


def is_fillable(side: str, entry: float | None, price: float) -> bool:
    """Would a limit at ``entry`` fill against ``price`` right now?

    A long fills on the way down and a short on the way up. A plan with no
    entry is treated as fillable so a missing level can never park a trade
    here silently.
    """
    if not entry or entry <= 0 or price <= 0:
        return True
    return price <= entry if side == "long" else price >= entry


def plan_is_dead(side: str, stop_loss: float, price: float) -> bool:
    """Has price run past the stop while the plan waited?

    A gap through the entry can carry price beyond the stop as well. Filling
    there would open a position that is already beyond where the plan said to
    give up, so the plan is spent rather than ready.
    """
    if stop_loss <= 0 or price <= 0:
        return False
    return price <= stop_loss if side == "long" else price >= stop_loss


def _hours_since(ts: str | None, now: datetime) -> float:
    if not ts:
        return 0.0
    try:
        started = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return 0.0
    return (now - started).total_seconds() / 3600.0


def record(suggestion: Suggestion, *, cycle_id: str | None) -> int | None:
    """Park a plan whose entry the market has not reached.

    Replaces any plan already waiting on the same product — a new read of the
    same chart supersedes the old one and restarts its clock.
    """
    if suggestion.action not in TRADE_ACTIONS:
        return None
    if suggestion.entry is None or suggestion.stop_loss is None:
        return None

    side = side_of(suggestion.action)
    with _connect() as conn:
        conn.execute(
            "DELETE FROM live_pending WHERE product_id = ?",
            (suggestion.product_id,),
        )
        cur = conn.execute(
            """
            INSERT INTO live_pending (
                cycle_id, product_id, action, side, entry, stop_loss,
                take_profits_json, risk_reward, size, order_block_ref,
                entry_tranche, rationale, created_at, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                suggestion.product_id,
                suggestion.action,
                side,
                float(suggestion.entry),
                float(suggestion.stop_loss),
                json.dumps(list(suggestion.take_profits or [])),
                suggestion.risk_reward,
                suggestion.size,
                suggestion.order_block_ref,
                suggestion.entry_tranche,
                suggestion.rationale,
                _now(),
                _now(),
            ),
        )
    logger.info(
        "live: %s %s plan waiting at %.2f (stop %.2f) — cycle %s",
        suggestion.product_id,
        side,
        float(suggestion.entry),
        float(suggestion.stop_loss),
        cycle_id,
    )
    return int(cur.lastrowid or 0)


def cancel(product_id: str, *, reason: str = "no setup this cycle") -> int:
    """Drop a waiting plan once Eva re-reads the chart and declines it.

    This, not the expiry clock, is what actually bounds a plan's life. Every
    cycle evaluates every traded product, so a ``no_trade`` is a fresh verdict
    on the same chart rather than silence, and holding the old limit through it
    would rest an order Eva would no longer write.
    """
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM live_pending WHERE product_id = ?", (product_id,)
        )
        dropped = int(cur.rowcount or 0)
    if dropped:
        logger.info("live: %s pending entry cancelled — %s", product_id, reason)
    return dropped


def get_pending(product_id: str | None = None) -> list[dict[str, Any]]:
    with _connect() as conn:
        if product_id:
            rows = conn.execute(
                "SELECT * FROM live_pending WHERE product_id = ?", (product_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM live_pending ORDER BY created_at ASC, id ASC"
            ).fetchall()
    return [dict(r) for r in rows]


def _suggestion_from(row: dict[str, Any]) -> Suggestion:
    try:
        tps = [float(x) for x in json.loads(row.get("take_profits_json") or "[]")]
    except (TypeError, ValueError):
        tps = []
    return Suggestion(
        action=str(row["action"]),
        size=float(row.get("size") or 0.0),
        entry=float(row["entry"]),
        stop_loss=float(row["stop_loss"]),
        take_profits=tps,
        risk_reward=row.get("risk_reward"),
        rationale=str(row.get("rationale") or ""),
        order_block_ref=row.get("order_block_ref"),
        entry_tranche=row.get("entry_tranche"),
        product_id=str(row["product_id"]),
    )


def _drop(pending_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM live_pending WHERE id = ?", (pending_id,))


def sweep(spots: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Fill the plans price has reached and retire the ones that went stale.

    Runs on the watchdog's cadence rather than the trade cycle's, because the
    fills that matter land within minutes of the plan being written — waiting
    for the next cycle boundary would miss most of them and leave the rule
    buying the mark half an hour late, which is the behaviour it exists to
    remove.

    A touched plan is dropped whether or not the order actually went on. The
    entry traded once; if a halt or a full sleeve stopped us taking it, that
    moment has passed, and holding the plan to fill later means filling at a
    worse price than the one being waited for.
    """
    if config.EXECUTION_MODE == "off" or not bot_config.LIVE_PENDING_ENTRIES_ENABLED:
        return []

    rows = get_pending()
    if not rows:
        return []

    if spots is None:
        import research

        spots = research.get_spot_prices()

    now_dt = datetime.now(timezone.utc)
    ttl = max(float(bot_config.LIVE_PENDING_EXPIRY_HOURS or 0), 0.0)
    filled: list[dict[str, Any]] = []

    for row in rows:
        pending_id = int(row["id"])
        product_id = str(row["product_id"])
        side = str(row["side"])
        entry = float(row["entry"])
        spot = float(spots.get(product_id) or 0)
        if spot <= 0:
            continue

        if is_fillable(side, entry, spot):
            _drop(pending_id)
            if plan_is_dead(side, float(row["stop_loss"]), spot):
                logger.info(
                    "live: %s %s plan at %.2f skipped — mark %.2f is already "
                    "through the stop %.2f",
                    product_id,
                    side,
                    entry,
                    spot,
                    float(row["stop_loss"]),
                )
                continue
            result = _fire(row, spot)
            if result is not None:
                filled.append(result)
            continue

        if ttl and _hours_since(row.get("created_at"), now_dt) >= ttl:
            _drop(pending_id)
            logger.info(
                "live: %s %s limit %.2f expired unfilled after %.1fh",
                product_id,
                side,
                entry,
                ttl,
            )
            continue

        with _connect() as conn:
            conn.execute(
                "UPDATE live_pending SET checked_at = ? WHERE id = ?",
                (_now(), pending_id),
            )

    return filled


def _fire(row: dict[str, Any], spot: float) -> dict[str, Any] | None:
    """Send the entry now that price has come to it."""
    import execute

    suggestion = _suggestion_from(row)
    logger.info(
        "live: %s %s entry %.2f reached at %.2f — sending order",
        row["product_id"],
        row["side"],
        float(row["entry"]),
        spot,
    )
    return execute.maybe_execute_live(
        suggestion,
        spot,
        cycle_id=row.get("cycle_id"),
        source="hq",
        fill_type="auto",
    )
