"""Payload for the password-gated Investor Analytics tab.

Every book the shop runs, measured the way the analysis pack measures it
(trade_ideas/analysis/scripts/_q0923_equity_edge.py): one position = one
observation (HQ paper ladder legs collapsed by position), P(edge>0) from a
day-clustered bootstrap of daily P&L, Sharpe annualized from daily P&L and
only quoted at >= 5 trading days. The scaling curves reuse the method of the
evidence pack's kalshi_scale_analysis chart: trades/day x per-contract edge
net of Kalshi's fee formula (0.07*p*(1-p)) and a 0.5c spread haircut, with
the participation cap from the recorded order-book depth study.

Computation is cached for CACHE_TTL_SEC because the bootstrap is the
expensive part and investors refresh in bursts.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 600
BOOT_DRAWS = 20000
MIN_SHARPE_DAYS = 5
KALSHI_EPOCH = {
    "eva_wick": "2026-09-17",
    "eva_streak": "2026-09-08T18:00:00Z",
    "eva_arb": "2026-09-08T18:00:00Z",
}
# 0.5% of median recorded window volume, blended BTC/ETH — from the
# order-book depth study behind kalshi_scale_analysis.png (2026-09-16).
KALSHI_PARTICIPATION_CAP_CT = 7257
SPREAD_HAIRCUT_USD = 0.005
SCALING_SIZES = (1, 2, 5, 10, 25, 50, 100, 200, 500, 1000, 2000)

_cache: dict[str, Any] = {"at": 0.0, "payload": None}


def _iso(ts: Any) -> str:
    return str(ts).replace(" ", "T")


def _epoch_ms(ts: Any) -> int:
    s = str(ts).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _day(ts: Any) -> str:
    return str(ts)[:10]


def _stats(rows: list[tuple[Any, float]], *, rng: random.Random) -> dict[str, Any]:
    """rows = [(closed_at, pnl)] -> table row + equity series."""
    rows = sorted(rows, key=lambda r: str(r[0]))
    pnls = [float(p) for _, p in rows]
    n = len(pnls)
    if n == 0:
        return {"n": 0, "days": 0, "total": 0.0, "win_pct": None,
                "p_edge": None, "sharpe": None, "equity": []}
    by_day: dict[str, float] = defaultdict(float)
    for t, p in rows:
        by_day[_day(t)] += float(p)
    daily = list(by_day.values())
    k = len(daily)
    p_edge = None
    if k >= 2:
        pos = 0
        for _ in range(BOOT_DRAWS):
            s = 0.0
            for _ in range(k):
                s += daily[rng.randrange(k)]
            if s > 0:
                pos += 1
        p_edge = round(pos / BOOT_DRAWS, 2)
    sharpe = None
    if k >= MIN_SHARPE_DAYS:
        mu = sum(daily) / k
        var = sum((x - mu) ** 2 for x in daily) / (k - 1)
        if var > 0:
            sharpe = round(mu / sqrt(var) * sqrt(252), 1)
    cum = 0.0
    equity = []
    for t, p in rows:
        cum += float(p)
        equity.append([_epoch_ms(t), round(cum, 2)])
    return {
        "n": n,
        "days": k,
        "total": round(sum(pnls), 2),
        "win_pct": round(100 * sum(1 for p in pnls if p > 0) / n),
        "p_edge": p_edge,
        "sharpe": sharpe,
        "equity": equity,
    }


def _hub_books(conn: sqlite3.Connection) -> dict[str, list[tuple[str, float]]]:
    books: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for src, closed_at, pnl in conn.execute(
        "SELECT source, closed_at, COALESCE(realized_pnl_usd, pnl_usd) "
        "FROM live_trades WHERE closed_at IS NOT NULL "
        "AND (source LIKE 'hq%' OR source = 'mill')"
    ):
        books[f"live:{src}"].append((_iso(closed_at), float(pnl or 0.0)))

    for variant, closed_at, pnl in conn.execute(
        "SELECT variant, closed_at, realized_pnl_usd FROM variant_positions "
        "WHERE status = 'closed' AND realized_pnl_usd IS NOT NULL"
    ):
        books[f"lab:{variant}"].append((_iso(closed_at), float(pnl)))

    # HQ paper control: per-leg 'close' rows collapsed to one obs per position.
    pos = {
        int(r[0]): (str(r[1]), float(r[2]))
        for r in conn.execute(
            "SELECT id, side, avg_entry FROM paper_positions "
            "WHERE status LIKE 'closed%'"
        )
    }
    legs: dict[int, float] = defaultdict(float)
    close_t: dict[int, str] = {}
    for pid, ts, price, qty, eth_qty in conn.execute(
        "SELECT position_id, ts, price, qty, eth_qty FROM paper_trades "
        "WHERE event = 'close' AND position_id IS NOT NULL ORDER BY id"
    ):
        p = pos.get(pid)
        if p is None:
            continue
        side, avg_entry = p
        q = float(qty or eth_qty or 0.0)
        d = 1.0 if side.lower() in ("long", "buy") else -1.0
        legs[pid] += (float(price) - avg_entry) * q * d
        close_t[pid] = _iso(ts)
    books["paper:hq_control"] = [
        (close_t[pid], round(v, 4)) for pid, v in legs.items() if pid in close_t
    ]

    # YieldGen: daily NAV marks -> daily $ deltas.
    navs = [
        (str(r[0]), float(r[1]))
        for r in conn.execute(
            "SELECT snapshot_date, nav_usd FROM yield_nav_snapshots ORDER BY 1"
        )
    ]
    books["yield:nav"] = [
        (f"{navs[i][0]}T23:59:00Z", round(navs[i][1] - navs[i - 1][1], 2))
        for i in range(1, len(navs))
    ]
    return books


def _kalshi_books(path: Path) -> dict[str, Any]:
    books: dict[str, list[tuple[str, float]]] = {}
    meta: dict[str, dict[str, float]] = {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for bot, epoch in KALSHI_EPOCH.items():
            rows = conn.execute(
                "SELECT opened_at, closed_at, pnl_usd, contracts, entry_cents "
                "FROM paper_positions WHERE bot_id = ? AND opened_at >= ? "
                "AND pnl_usd IS NOT NULL AND closed_at IS NOT NULL",
                (bot, epoch),
            ).fetchall()
            books[f"kalshi:{bot}"] = [
                (_iso(c), float(p)) for o, c, p, _, _ in rows
            ]
            n_ct = sum(int(ct or 0) for _, _, _, ct, _ in rows)
            opened_days = {_day(o) for o, _, _, _, _ in rows}
            weekday = [r for r in rows if datetime.fromisoformat(
                str(r[0]).replace("Z", "+00:00")).weekday() < 5]
            wd_ct = sum(int(ct or 0) for _, _, _, ct, _ in weekday)
            wd_days = {_day(r[0]) for r in weekday}
            meta[bot] = {
                "usd_per_ct": (sum(float(p) for _, _, p, _, _ in rows) / n_ct)
                if n_ct else 0.0,
                "wd_usd_per_ct": (sum(float(r[2]) for r in weekday) / wd_ct)
                if wd_ct else 0.0,
                "trades_per_day": len(rows) / max(len(opened_days), 1),
                "wd_trades_per_day": len(weekday) / max(len(wd_days), 1),
                "avg_entry_cents": (
                    sum(float(e or 0) for _, _, _, _, e in rows) / len(rows)
                ) if rows else 0.0,
            }
    finally:
        conn.close()
    return {"books": books, "meta": meta}


def _kalshi_fee_per_ct(entry_cents: float) -> float:
    p = max(0.01, min(0.99, entry_cents / 100.0))
    return 0.07 * p * (1 - p)


def _scaling(meta: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Expected $/day vs contracts/trade for the books worth scaling.

    eva_arb is deliberately absent: its window (last 2 minutes) is where the
    book thins and the recorded fill study showed the mid is fiction there.
    """
    out = []
    specs = [
        ("eva_wick", "Kalshi wick — as traded", "usd_per_ct", "trades_per_day"),
        ("eva_wick", "Kalshi wick — weekday (gate in-sample)",
         "wd_usd_per_ct", "wd_trades_per_day"),
        ("eva_streak", "Kalshi reversal — weekday (gate in-sample)",
         "wd_usd_per_ct", "wd_trades_per_day"),
    ]
    for bot, label, edge_key, tpd_key in specs:
        m = meta.get(bot)
        if not m:
            continue
        fee = _kalshi_fee_per_ct(m["avg_entry_cents"])
        # ledger P&L is pre-fee; net out the fee and a spread haircut
        net = m[edge_key] - fee - SPREAD_HAIRCUT_USD
        tpd = m[tpd_key]
        out.append({
            "label": label,
            "gross_usd_per_ct": round(m[edge_key], 4),
            "fee_usd_per_ct": round(fee, 4),
            "net_usd_per_ct": round(net, 4),
            "trades_per_day": round(tpd, 1),
            "sizes": list(SCALING_SIZES),
            "usd_per_day": [round(tpd * net * s, 2) for s in SCALING_SIZES],
        })
    return {
        "cap_ct": KALSHI_PARTICIPATION_CAP_CT,
        "cap_note": (
            "participation cap ~ 0.5% of median recorded window volume "
            "(depth study, 2026-09-16). Curves assume the per-contract edge "
            "holds at size, which is unmeasured above 8 contracts — the "
            "stepped size-up plan exists to measure exactly that."
        ),
        "spread_haircut_usd": SPREAD_HAIRCUT_USD,
        "books": out,
    }


_LABELS = {
    "paper:hq_control": ("HQ Paper Control", "hub"),
    "live:hq": ("HQ Live", "hub"),
    "live:hq_swing": ("HQ Swing mirror (live)", "hub"),
    "live:hq_day": ("HQ Day mirror (live)", "hub"),
    "live:mill": ("Trade Mill (live)", "product"),
    "lab:eva_swing_llm": ("Lab swing (LLM exits)", "lab"),
    "lab:eva_swing_mech": ("Lab swing (mech exits)", "lab"),
    "lab:eva_day": ("Lab day", "lab"),
    "lab:eva_geom": ("Lab geometry", "lab"),
    "yield:nav": ("YieldGen (NAV, live)", "product"),
    "kalshi:eva_wick": ("Kalshi wick (LIVE)", "kalshi"),
    "kalshi:eva_streak": ("Kalshi reversal (paper)", "kalshi"),
    "kalshi:eva_arb": ("Kalshi arb (paper)", "kalshi"),
}
_ORDER = list(_LABELS)


def build_edge_payload() -> dict[str, Any]:
    now = time.time()
    if _cache["payload"] is not None and now - _cache["at"] < CACHE_TTL_SEC:
        return _cache["payload"]

    rng = random.Random(20260925)
    conn = sqlite3.connect(f"file:{config.LEDGER_DB}?mode=ro", uri=True)
    try:
        books = _hub_books(conn)
    finally:
        conn.close()

    scaling: dict[str, Any] | None = None
    try:
        import kalshi_bridge

        kpath = kalshi_bridge.kalshi_db_path()
        if kpath and Path(kpath).exists():
            k = _kalshi_books(Path(kpath))
            books.update(k["books"])
            scaling = _scaling(k["meta"])
    except Exception:
        logger.exception("edge analytics: kalshi ledger unavailable")

    table = []
    for key in _ORDER:
        rows = books.get(key)
        if not rows:
            continue
        label, group = _LABELS[key]
        stat = _stats(rows, rng=rng)
        table.append({"key": key, "label": label, "group": group, **stat})

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache_ttl_sec": CACHE_TTL_SEC,
        "books": table,
        "scaling": scaling,
        "method": (
            "One position = one observation (HQ paper ladder legs collapsed). "
            "P(edge>0): day-clustered bootstrap, 20k resamples of daily P&L. "
            "Sharpe: annualized from daily P&L, quoted only at >=5 trading "
            "days. Kalshi ledgers are pre-fee. Small day-cluster counts — "
            "read as directional measurements, not conclusive statistics."
        ),
    }
    _cache.update(at=now, payload=payload)
    return payload
