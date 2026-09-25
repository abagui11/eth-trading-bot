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

import bot_config
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

# Sleeve / seed used to turn $ P&L into percent growth. Lab has no formal
# sleeve — $1k notional so the curve is readable as book %. Kalshi seeds
# match paper_state cash_start on the live wick / paper streak / arb books.
# Mill house paper is already stored as pnl_pct (same unit as the daily
# "you'd be up X%" digest); base 100 makes cum/base*100 = cum of those %.
HQ_PAPER_BASE_USD = 5000.0
KALSHI_SEED_USD = 246.75
LAB_NOTIONAL_USD = 1000.0
MILL_PAPER_PCT_BASE = 100.0

_cache: dict[str, Any] = {"at": 0.0, "payload": None}


def _base_usd(key: str) -> float | None:
    """Capital base for percent growth. Yield bases come from the first NAV."""
    return {
        "paper:hq_control": HQ_PAPER_BASE_USD,
        "live:hq": float(bot_config.LIVE_HQ_EQUITY_USD),
        "live:hq_swing": float(bot_config.LIVE_HQ_EQUITY_USD),
        "live:hq_day": float(bot_config.LIVE_HQ_EQUITY_USD),
        "paper:mill": MILL_PAPER_PCT_BASE,
        "lab:eva_swing_llm": LAB_NOTIONAL_USD,
        "lab:eva_swing_mech": LAB_NOTIONAL_USD,
        "lab:eva_day": LAB_NOTIONAL_USD,
        "lab:eva_geom": LAB_NOTIONAL_USD,
        "kalshi:eva_wick": KALSHI_SEED_USD,
        "kalshi:eva_streak": KALSHI_SEED_USD,
        "kalshi:eva_arb": KALSHI_SEED_USD,
    }.get(key)


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


def _stats(
    rows: list[tuple[Any, float]],
    *,
    base_usd: float,
    rng: random.Random,
) -> dict[str, Any]:
    """rows = [(closed_at, pnl_usd)] -> table row + percent-growth equity.

    Equity and total are percent of ``base_usd`` (sleeve / seed / notional).
    Bootstrap and Sharpe still run on daily $ P&L — the sign of edge is
    unchanged; only the display unit is percent growth.
    """
    rows = sorted(rows, key=lambda r: str(r[0]))
    pnls = [float(p) for _, p in rows]
    n = len(pnls)
    base = float(base_usd)
    if n == 0 or base <= 0:
        return {"n": 0, "days": 0, "total": 0.0, "base_usd": round(base, 2),
                "win_pct": None, "p_edge": None, "sharpe": None, "equity": []}
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
        equity.append([_epoch_ms(t), round(100.0 * cum / base, 2)])
    total_usd = sum(pnls)
    return {
        "n": n,
        "days": k,
        "total": round(100.0 * total_usd / base, 2),
        "base_usd": round(base, 2),
        "win_pct": round(100 * sum(1 for p in pnls if p > 0) / n),
        "p_edge": p_edge,
        "sharpe": sharpe,
        "equity": equity,
    }


def _pct_equity_from_levels(
    levels: list[tuple[str, float]],
) -> tuple[list[list[float]], float, float]:
    """Absolute level marks -> percent-growth equity starting at 0.

    Returns (equity, total_pct, base_level).
    """
    if not levels:
        return [], 0.0, 0.0
    base = float(levels[0][1])
    if base <= 0:
        return [], 0.0, 0.0
    equity = [
        [_epoch_ms(t), round(100.0 * (float(v) / base - 1.0), 2)]
        for t, v in levels
    ]
    total = round(100.0 * (float(levels[-1][1]) / base - 1.0), 2)
    return equity, total, base


def _hub_books(
    conn: sqlite3.Connection,
) -> tuple[
    dict[str, list[tuple[str, float]]],
    dict[str, list[tuple[str, float]]],
    dict[str, float],
]:
    """Trade P&L books plus YieldGen series for % equity.

    YieldGen contributes daily $ deltas in ``books`` for bootstrap stats,
    NAV level marks in ``yield_levels`` (chart starts at 0%, tracks
    NAV/NAV₀), and ``yield_bases`` carrying the NAV₀ base for the carry
    book, whose rows are deltas rather than levels.

    Trade Mill's live clips are omitted here — Investor Analytics uses the
    full sized-idea book via ``_mill_paper_book`` (live fills are a capital-
    limited subset of that book).
    """
    books: dict[str, list[tuple[str, float]]] = defaultdict(list)
    yield_levels: dict[str, list[tuple[str, float]]] = {}
    yield_bases: dict[str, float] = {}
    for src, closed_at, pnl in conn.execute(
        "SELECT source, closed_at, COALESCE(realized_pnl_usd, pnl_usd) "
        "FROM live_trades WHERE closed_at IS NOT NULL "
        "AND source LIKE 'hq%'"
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

    # YieldGen: USD NAV (total, deliberately ETH-long) + carry ex-ETH (what
    # the strategy earns on top of the ride). ETH-denominated NAV was dropped:
    # it benchmarks against beta=1, and the book runs ~0.7 net ETH exposure
    # on purpose, so that line is mechanically down in a rally.
    navs = [
        (str(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        for r in conn.execute(
            "SELECT snapshot_date, nav_usd, eth_price_usd, "
            "collateral_usd, debt_usd FROM yield_nav_snapshots "
            "WHERE eth_price_usd IS NOT NULL AND eth_price_usd > 0 "
            "ORDER BY 1"
        )
    ]
    if len(navs) >= 2:
        usd_levels = [(f"{d}T23:59:00Z", nav) for d, nav, _, _, _ in navs]
        yield_levels["yield:nav"] = usd_levels
        books["yield:nav"] = [
            (usd_levels[i][0], round(usd_levels[i][1] - usd_levels[i - 1][1], 4))
            for i in range(1, len(usd_levels))
        ]
        carry = _yield_carry(navs)
        if carry:
            books["yield:carry"] = carry
            yield_bases["yield:carry"] = navs[0][1]  # NAV at first snapshot
    return books, yield_levels, yield_bases


def _yield_carry(
    navs: list[tuple[str, float, float, float, float]],
) -> list[tuple[str, float]]:
    """Daily ex-ETH carry: ΔNAV minus (net ETH exposure × ETH move).

    navs = [(date, nav_usd, eth_price, collateral_usd, debt_usd)].

    Net exposure per day = collateral ETH units − borrowed ETH units. The
    snapshots don't store the debt split, but the recorded debt decomposes as
    ``stables + units×eth_price`` almost exactly (fit residual < $2 on ~$1.7k
    over the 09-25 window), so the borrowed units come from that fit. If the
    debt structure stops fitting (residual > 1% of median debt), the carry
    book is dropped rather than published wrong.
    """
    if len(navs) < 3:
        return []
    es = [r[2] for r in navs]
    ds = [r[4] for r in navs]
    n = len(navs)
    me = sum(es) / n
    md = sum(ds) / n
    var = sum((e - me) ** 2 for e in es)
    if var <= 1e-9:  # ETH price flat across window: split is unidentifiable
        return []
    b = sum((e - me) * (d - md) for e, d in zip(es, ds)) / var
    resid = max(abs(d - (md + b * (e - me))) for e, d in zip(es, ds))
    med_debt = sorted(ds)[n // 2]
    if med_debt > 0 and resid > 0.01 * med_debt:
        logger.warning(
            "yield carry: debt no longer fits stables+ETH split "
            "(resid %.1f); omitting carry book", resid,
        )
        return []
    out: list[tuple[str, float]] = []
    for i in range(1, n):
        day, nav1, e1 = navs[i][0], navs[i][1], navs[i][2]
        nav0, e0, col0 = navs[i - 1][1], navs[i - 1][2], navs[i - 1][3]
        units = col0 / e0 - b  # net ETH exposure held into day i
        carry = (nav1 - nav0) - units * (e1 - e0)
        out.append((f"{day}T23:59:00Z", round(carry, 4)))
    return out


_MILL_PAPER_CLOSED = frozenset({"hit_tp", "hit_sl"})


def _mill_paper_book() -> list[tuple[str, float]]:
    """Trade Mill closes since ``MILL_PAPER_EPOCH_START`` (every sized idea).

    Each row's ``pnl_pct`` is already a percent-of-notional return (same unit
    as the daily digest). Live fills are a capital-limited subset of this book.
    """
    try:
        import trade_ideas_bridge
    except Exception:
        logger.exception("edge analytics: trade_ideas_bridge unavailable")
        return []
    since = str(bot_config.MILL_PAPER_EPOCH_START or "").strip()
    if not since:
        return []
    trades = trade_ideas_bridge.mill_paper_trades_since(since)
    out: list[tuple[str, float]] = []
    for t in trades:
        if str(t.get("status") or "") not in _MILL_PAPER_CLOSED:
            continue
        closed_at = t.get("closed_at")
        if not closed_at:
            continue
        pnl = t.get("pnl_pct")
        if pnl is None:
            continue
        out.append((_iso(closed_at), float(pnl)))
    return out


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
    "paper:mill": ("Trade Mill", "product"),
    "lab:eva_swing_llm": ("Lab swing (LLM exits)", "lab"),
    "lab:eva_swing_mech": ("Lab swing (mech exits)", "lab"),
    "lab:eva_day": ("Lab day", "lab"),
    "lab:eva_geom": ("Lab geometry", "lab"),
    "yield:nav": ("YieldGen (USD, incl. ETH ride)", "product"),
    "yield:carry": ("YieldGen carry (ex-ETH)", "product"),
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
        books, yield_levels, yield_bases = _hub_books(conn)
    finally:
        conn.close()

    try:
        mill_rows = _mill_paper_book()
        if mill_rows:
            books["paper:mill"] = mill_rows
    except Exception:
        logger.exception("edge analytics: mill paper book unavailable")

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
        if key in yield_levels:
            levels = yield_levels[key]
            equity, total, base = _pct_equity_from_levels(levels)
            # Bootstrap / Sharpe still need the day-to-day deltas; pass the
            # level-derived base so total matches the absolute growth series.
            stat = _stats(rows, base_usd=base, rng=rng)
            stat["equity"] = equity
            stat["total"] = total
            stat["base_usd"] = round(base, 2)
        elif key in yield_bases:
            stat = _stats(rows, base_usd=yield_bases[key], rng=rng)
        else:
            base = _base_usd(key)
            if base is None or base <= 0:
                continue
            # Mill paper rows are already pnl_pct; base 100 → cum displays as %.
            stat = _stats(rows, base_usd=base, rng=rng)
        table.append({"key": key, "label": label, "group": group, **stat})

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache_ttl_sec": CACHE_TTL_SEC,
        "books": table,
        "scaling": scaling,
        "method": (
            "One position = one observation (HQ paper ladder legs collapsed). "
            "Charts and Return % are percent growth of each book's sleeve/seed "
            f"(HQ paper ${HQ_PAPER_BASE_USD:.0f}, HQ live "
            f"${float(bot_config.LIVE_HQ_EQUITY_USD):.0f}, Kalshi "
            f"${KALSHI_SEED_USD:.2f}, lab ${LAB_NOTIONAL_USD:.0f} notional). "
            "Trade Mill is every sized idea since "
            f"{bot_config.MILL_PAPER_EPOCH_START} (sum of per-idea pnl_pct — "
            "same unit as the daily digest); live fills are a capital-limited "
            "subset of that book. "
            "YieldGen USD is NAV/NAV₀ — the book deliberately runs ~0.7 net "
            "ETH exposure, so this line includes the intended ETH ride. "
            "YieldGen carry strips it: ΔNAV − (net ETH exposure × ETH move), "
            "cumulated daily as % of NAV₀ — the yield the strategy earns "
            "regardless of ETH direction. Net exposure = collateral ETH "
            "units − borrowed ETH units (debt fits stables+ETH almost "
            "exactly; carry is omitted if that split stops fitting). "
            "P(edge>0): day-clustered bootstrap, 20k resamples of daily P&L. "
            "Sharpe: annualized from daily P&L, quoted only at >=5 trading "
            "days. Kalshi ledgers are pre-fee. Small day-cluster counts — "
            "read as directional measurements, not conclusive statistics."
        ),
    }
    _cache.update(at=now, payload=payload)
    return payload
