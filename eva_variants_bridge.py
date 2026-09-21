"""Dashboard payload for the Eva variant experiment — four books, one live.

Mirrors ``kalshi_bridge.performance_payload``: one call returns every book with
a ``mode`` of live or paper, so the template renders them uniformly and the
live book is whichever one ``EVA_LIVE_VARIANT`` names.

Control's numbers are read **read-only** out of ``paper_positions`` /
``paper_trades``. Two things that would otherwise corrupt the comparison are
handled here explicitly:

* ``paper_trades`` holds one close row *per ladder leg*, so a 3-target winner
  appears three times. Rows are grouped by ``position_id`` before anything is
  counted — ungrouped, control's win rate is inflated by exactly the trades
  that worked best.
* Books are compared in **R**, not dollars. Control sizes off a portfolio
  fraction and the variants off a fixed risk budget, so dollar P&L is not
  comparable between them; R is.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import bot_config
import config
import eva_variants

logger = logging.getLogger(__name__)


def _epoch() -> str:
    return str(getattr(bot_config, "EVA_EXPERIMENT_EPOCH", "1970-01-01"))


def _live_variant() -> str:
    return str(getattr(bot_config, "EVA_LIVE_VARIANT", eva_variants.CONTROL))


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def was_stopped_out(row: dict) -> bool:
    """True only for exits at the *opening* stop, with no rung banked.

    The two engines label trailed exits differently — ``paper`` reports every
    stop-shaped exit as ``stop_loss`` even once the stop has trailed to
    breakeven, while the variant engine says ``trail``. Deciding on the label
    alone would count control's trailed winners as stopped and the variants'
    as not, biasing the stopped-then-paid rate the pre-registration reads as a
    primary metric. ``tps_hit`` settles it the same way for both books.
    """
    if "stop" not in str(row.get("close_reason") or "").lower():
        return False
    return int(row.get("tps_hit") or 0) == 0


def _control_stops() -> dict[str, dict]:
    """Opening stop and MFE per control position, keyed by ``open_cycle_id``.

    R needs the stop the position was opened with, which lives on
    ``paper_positions`` rather than in the trade log.
    """
    out: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(f"file:{config.LEDGER_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        logger.exception("control book: cannot open ledger read-only")
        return out
    try:
        for r in conn.execute(
            "SELECT open_cycle_id, stop_loss, avg_entry, tps_hit, mfe_pct"
            " FROM paper_positions"
        ):
            key = str(r["open_cycle_id"] or "")
            if key:
                out[key] = dict(r)
    except sqlite3.Error:
        logger.exception("control book: paper_positions query failed")
    finally:
        conn.close()
    return out


def control_positions(since: str | None = None) -> list[dict]:
    """Control's closed positions, ladder legs collapsed, expressed in R.

    Pairing is delegated to ``paper._pair_closed_trades`` via
    ``paper.get_closed_trades`` rather than reimplemented here. That matters:
    ``position_id`` is NULL on every ``open`` row in ``paper_trades``, so
    grouping on it silently drops whole positions. ``paper`` already solved
    this with LIFO matching that also handles scale-ins and partial closes.

    Its output is still one row *per scale-out leg*, so rows are then grouped
    by ``open_cycle_id`` — without that a 3-target winner counts three times
    and control's win rate is inflated by exactly its best trades.
    """
    since = since or _epoch()
    try:
        import paper

        legs = paper.get_closed_trades(limit=100_000)
    except Exception:
        logger.exception("control book: get_closed_trades failed")
        return []

    stops = _control_stops()

    grouped: dict[str, list[dict]] = {}
    for leg in legs:
        key = str(leg.get("open_cycle_id") or f"_leg{id(leg)}")
        grouped.setdefault(key, []).append(leg)

    out: list[dict] = []
    for cycle_id, rows in grouped.items():
        rows = sorted(rows, key=lambda r: str(r.get("closed_at") or ""))
        qty = sum(float(r.get("qty") or r.get("eth_qty") or 0) for r in rows)
        if qty <= 0:
            continue
        entry = sum(
            float(r.get("qty") or r.get("eth_qty") or 0) * float(r["entry"])
            for r in rows
        ) / qty
        pnl = sum(float(r.get("realized_pnl_usd") or 0) for r in rows)

        meta = stops.get(cycle_id) or {}
        stop = float(meta.get("stop_loss") or 0)
        if stop <= 0:
            continue
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            continue

        closed_at = str(rows[-1].get("closed_at") or "")
        if since and closed_at < since:
            continue
        opened_at = str(rows[0].get("opened_at") or "")
        a, b = _parse(opened_at), _parse(closed_at)
        mfe_pct = meta.get("mfe_pct")
        reasons = [str(r.get("close_reason") or "") for r in rows]
        out.append({
            "variant": eva_variants.CONTROL,
            "product_id": str(rows[0].get("product_id") or "ETH-USD"),
            "side": str(rows[0].get("side") or "long"),
            "entry": entry,
            "stop_loss": stop,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "exit_price": float(rows[-1].get("exit") or 0),
            "close_reason": reasons[-1],
            "realized_r": pnl / (qty * risk_per_unit),
            "realized_pnl_usd": pnl,
            "tps_hit": int(meta.get("tps_hit") or 0),
            "mfe_r": (float(mfe_pct) / 100.0 * entry / risk_per_unit
                      if mfe_pct is not None else None),
            "hold_h": ((b - a).total_seconds() / 3600 if a and b else None),
        })
    out.sort(key=lambda r: str(r["closed_at"]), reverse=True)
    return out


def control_summary(since: str | None = None) -> dict:
    rows = control_positions(since)
    rs = [float(r["realized_r"]) for r in rows]
    holds = [r["hold_h"] for r in rows if r["hold_h"] is not None]
    stopped = [r for r in rows if was_stopped_out(r)]
    paid = [r for r in stopped
            if r["mfe_r"] is not None and float(r["mfe_r"]) > 0]
    return {
        "variant": eva_variants.CONTROL,
        "label": eva_variants.LABELS[eva_variants.CONTROL],
        "blurb": eva_variants.BLURBS[eva_variants.CONTROL],
        "n_closed": len(rows),
        "n_open": 0,
        "win_rate": (len([r for r in rs if r > 0]) / len(rs)) if rs else None,
        "mean_r": (sum(rs) / len(rs)) if rs else None,
        "sum_r": sum(rs) if rs else 0.0,
        "pnl_usd": sum(float(r["realized_pnl_usd"]) for r in rows),
        "median_hold_h": _median(holds),
        "stopped_n": len(stopped),
        "stopped_then_paid": len(paid),
        "skips": 0,
    }


def _control_open_count() -> int:
    try:
        import paper

        return len(paper.get_open_positions())
    except Exception:
        logger.debug("control open count unavailable", exc_info=True)
        return 0


def performance_payload(limit: int = 20) -> dict[str, Any]:
    """Every book, plus open positions and recent closes, for the Eva Lab tab."""
    if not getattr(bot_config, "EVA_VARIANTS_ENABLED", False):
        return {"available": False}

    epoch = _epoch()
    live = _live_variant()
    limit = max(1, min(int(limit), 100))

    books: list[dict] = []
    try:
        control = control_summary(epoch)
        control["n_open"] = _control_open_count()
        books.append(control)
    except Exception:
        logger.exception("control summary failed")

    for name in eva_variants.WRITABLE:
        try:
            books.append(eva_variants.summary(name, since=epoch))
        except Exception:
            logger.exception("variant summary failed for %s", name)

    # Three-state mode since the 2026-09-21 prereg amendment: control is the
    # promoted live book; the two mirrored variants trade real money too but
    # are NOT promoted — the tab must say both things at once, because a live
    # badge that reads as a verdict is how an n=9 lead becomes "the strategy".
    mirrored = {
        "eva_swing_llm": getattr(bot_config, "EVA_SWING_LLM_LIVE_ENABLED", False),
        "eva_day": getattr(bot_config, "EVA_DAY_LIVE_ENABLED", False),
    }
    for b in books:
        if b["variant"] == live:
            b["mode"] = "live"
        elif mirrored.get(b["variant"]):
            b["mode"] = "live_mirror"
        else:
            b["mode"] = "paper"

    # Best paper book by mean R — the promotion candidate, *not* a
    # recommendation. Whether it has earned promotion is decided by the
    # pre-registered bar in EVA_VARIANTS_PREREG.md, not by leading this list.
    ranked = [b for b in books
              if b["mode"] == "paper" and b.get("mean_r") is not None
              and b["n_closed"] > 0]
    leader = max(ranked, key=lambda b: b["mean_r"])["variant"] if ranked else None

    try:
        open_positions = eva_variants.open_positions()
    except Exception:
        logger.exception("variant open positions failed")
        open_positions = []

    closed: list[dict] = []
    try:
        closed.extend(eva_variants.closed_positions(limit=limit))
    except Exception:
        logger.exception("variant closed positions failed")
    try:
        closed.extend(control_positions(epoch)[:limit])
    except Exception:
        logger.exception("control closed positions failed")
    closed.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)

    return {
        "available": True,
        "epoch": epoch,
        "live_variant": live,
        "risk_usd": eva_variants.VARIANT_RISK_USD,
        "leader": leader,
        "books": books,
        "open_positions": open_positions,
        "closed": closed[:limit],
    }
