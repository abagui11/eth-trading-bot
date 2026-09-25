"""Shadow table of eva_wick pairing variants for the Kalshi tab.

Each variant is a pure function of the recorded wick book (KALSHI_DB) and
the recorded quote log (KALSHI_LASTMIN_DB, full-window since 2026-09-17) —
no paper engine, no state, no trading-path code. Because they are derived,
they are retroactive to the epoch and accrue automatically as the book and
the quote log grow. This is the forward test for the 2026-09-25 pairing
studies (`trade_ideas/analysis/scripts/_q0925_wick_*.py`), which found every
variant inside noise at 9 day-clusters.

Variants (all reactions to the sibling asset qualifying in the same 15m
window):
  exit_first_opposite   sell the first leg at the recorded bid when the
                        sibling qualifies on the opposite side (fee charged)
  skip_second_opposite  never take the opposing second leg
  double_second_opposite duplicate the opposing second clip (own fee)
  double_second_aligned duplicate the aligned second clip (own fee)
  double_first_aligned  add a first-leg clip at its recorded ask at the
                        moment the aligned sibling enters (own fee)

Reported per variant: Δ P&L vs the live rule, window count, day clusters,
negative days, and P(Δ>0) from a day-clustered bootstrap of daily deltas.
The table states its own honesty rule: nothing here is actionable until the
bootstrap holds up at materially more day-clusters than the studies had.
"""

from __future__ import annotations

import logging
import random
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EPOCH = "2026-09-17"
MAX_QUOTE_AGE_SEC = 30.0
BOOT_DRAWS = 5000
CACHE_TTL_SEC = 600

_TICKER = re.compile(r"KX(BTC|ETH)15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

_cache: dict[str, Any] = {"at": 0.0, "payload": None}


def _iso(s: str) -> datetime:
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fee_usd(price_cents: float, contracts: int) -> float:
    p = price_cents / 100.0
    return ceil(0.07 * p * (1.0 - p) * contracts * 100.0) / 100.0


def _windows(conn: sqlite3.Connection) -> dict[str, dict[str, dict]]:
    """Closed wick legs since the epoch, grouped by 15m window."""
    wins: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT market_ticker, side, contracts, entry_cents, pnl_usd, "
        "opened_at FROM paper_positions "
        "WHERE bot_id='eva_wick' AND opened_at >= ? "
        "AND status != 'open' AND pnl_usd IS NOT NULL",
        (EPOCH,),
    ):
        row = dict(zip(
            ("market_ticker", "side", "contracts", "entry_cents",
             "pnl_usd", "opened_at"), r))
        m = _TICKER.match(row["market_ticker"])
        if not m:
            continue
        asset, yy, mon, dd, hh, mm = m.groups()
        key = f"20{yy}-{_MONTHS[mon]:02d}-{dd}T{hh}:{mm}"
        wins[key][asset] = row
    return wins


def _quote_near(
    qconn: sqlite3.Connection, ticker: str, at: datetime
) -> dict | None:
    lo = (at - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S")
    hi = (at + timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S")
    best, age = None, None
    for ts, bid, ask in qconn.execute(
        "SELECT ts, yes_bid, yes_ask FROM quotes "
        "WHERE ticker = ? AND ts BETWEEN ? AND ? ORDER BY ts",
        (ticker, lo, hi),
    ):
        a = abs((_iso(ts) - at).total_seconds())
        if best is None or a < age:
            best, age = {"yes_bid": bid, "yes_ask": ask}, a
    if best is None or age > MAX_QUOTE_AGE_SEC:
        return None
    return best


def _boot_p_positive(daily: list[float], *, rng: random.Random) -> float | None:
    k = len(daily)
    if k < 2:
        return None
    pos = 0
    for _ in range(BOOT_DRAWS):
        s = 0.0
        for _ in range(k):
            s += daily[rng.randrange(k)]
        if s > 0:
            pos += 1
    return round(pos / BOOT_DRAWS, 2)


def _summarise(deltas_by_day: dict[str, float], n_windows: int,
               *, rng: random.Random, skipped: int = 0) -> dict[str, Any]:
    daily = list(deltas_by_day.values())
    total = sum(daily)
    return {
        "n_windows": n_windows,
        "n_days": len(daily),
        "delta_usd": round(total, 2),
        "neg_days": sum(1 for v in daily if v < 0),
        "p_better": _boot_p_positive(daily, rng=rng),
        "skipped_no_quote": skipped,
    }


def build_variants_payload(
    kalshi_db: Path | None, lastmin_db: Path | None
) -> dict[str, Any]:
    now = time.time()
    if _cache["payload"] is not None and now - _cache["at"] < CACHE_TTL_SEC:
        return _cache["payload"]
    payload = _build(kalshi_db, lastmin_db)
    _cache.update(at=now, payload=payload)
    return payload


def _build(kalshi_db: Path | None, lastmin_db: Path | None) -> dict[str, Any]:
    if kalshi_db is None or not kalshi_db.exists():
        return {"available": False, "reason": "KALSHI_DB unset"}
    conn = sqlite3.connect(f"file:{kalshi_db}?mode=ro", uri=True, timeout=5.0)
    try:
        wins = _windows(conn)
    finally:
        conn.close()

    qconn = None
    if lastmin_db is not None and lastmin_db.exists():
        try:
            qconn = sqlite3.connect(
                f"file:{lastmin_db}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error:
            logger.exception("lastmin quote log unavailable")
            qconn = None

    opposite, aligned = [], []
    for legs in wins.values():
        if len(legs) != 2:
            continue
        pair = sorted(legs.values(), key=lambda r: r["opened_at"])
        if legs["BTC"]["side"] == legs["ETH"]["side"]:
            aligned.append(pair)
        else:
            opposite.append(pair)

    rng = random.Random(20260925)
    variants: list[dict[str, Any]] = []

    def add(key: str, label: str, rule: str, needs_quotes: bool,
            stats: dict[str, Any] | None) -> None:
        row: dict[str, Any] = {"key": key, "label": label, "rule": rule}
        if stats is None:
            row.update(available=False,
                       reason="quote log unavailable" if needs_quotes else "no data")
        else:
            row.update(available=True, **stats)
        variants.append(row)

    # -- skip_second_opposite: delta = -(second leg pnl) ------------------
    by_day: dict[str, float] = defaultdict(float)
    for first, second in opposite:
        by_day[second["opened_at"][:10]] -= float(second["pnl_usd"])
    add("skip_second_opposite", "Skip opposing 2nd leg",
        "never enter when the sibling window is already held the other way",
        False, _summarise(by_day, len(opposite), rng=rng))

    # -- double_second_opposite: duplicate clip, own fee ------------------
    by_day = defaultdict(float)
    for first, second in opposite:
        fee = _fee_usd(float(second["entry_cents"]), int(second["contracts"]))
        by_day[second["opened_at"][:10]] += float(second["pnl_usd"]) - fee
    add("double_second_opposite", "Double opposing 2nd leg",
        "duplicate the second clip when it contradicts the first",
        False, _summarise(by_day, len(opposite), rng=rng))

    # -- double_second_aligned: duplicate clip, own fee -------------------
    by_day = defaultdict(float)
    for first, second in aligned:
        fee = _fee_usd(float(second["entry_cents"]), int(second["contracts"]))
        by_day[second["opened_at"][:10]] += float(second["pnl_usd"]) - fee
    add("double_second_aligned", "Double aligned 2nd leg",
        "duplicate the second clip when it confirms the first",
        False, _summarise(by_day, len(aligned), rng=rng))

    # -- exit_first_opposite: sell first leg at recorded bid --------------
    if qconn is None:
        add("exit_first_opposite", "Exit 1st on contradiction",
            "sell the first leg at the bid when the sibling qualifies opposite",
            True, None)
        add("double_first_aligned", "Add to 1st on confirmation",
            "buy more of the first leg at the ask when the sibling aligns",
            True, None)
    else:
        by_day = defaultdict(float)
        skipped = 0
        for first, second in opposite:
            q = _quote_near(qconn, first["market_ticker"],
                            _iso(second["opened_at"]))
            px = None
            if q is not None:
                if first["side"] == "YES":
                    px = q["yes_bid"]
                elif q["yes_ask"] is not None:
                    px = 100.0 - q["yes_ask"]
            if px is None or px <= 0:
                skipped += 1
                continue
            ct = int(first["contracts"])
            fee = _fee_usd(px, ct)
            exit_pnl = (px - float(first["entry_cents"])) / 100.0 * ct - fee
            by_day[first["opened_at"][:10]] += exit_pnl - float(first["pnl_usd"])
        add("exit_first_opposite", "Exit 1st on contradiction",
            "sell the first leg at the bid when the sibling qualifies opposite",
            True, _summarise(by_day, len(opposite) - skipped,
                             rng=rng, skipped=skipped))

        by_day = defaultdict(float)
        skipped = 0
        for first, second in aligned:
            q = _quote_near(qconn, first["market_ticker"],
                            _iso(second["opened_at"]))
            px = None
            if q is not None:
                if first["side"] == "YES":
                    px = q["yes_ask"]
                elif q["yes_bid"] is not None:
                    px = 100.0 - q["yes_bid"]
            if px is None or not (0 < px < 100):
                skipped += 1
                continue
            ct = int(first["contracts"])
            won = float(first["pnl_usd"]) > 0
            p = px / 100.0
            gross = ((1.0 - p) if won else -p) * ct
            by_day[first["opened_at"][:10]] += gross - _fee_usd(px, ct)
        add("double_first_aligned", "Add to 1st on confirmation",
            "buy more of the first leg at the ask when the sibling aligns",
            True, _summarise(by_day, len(aligned) - skipped,
                             rng=rng, skipped=skipped))
        qconn.close()

    return {
        "available": True,
        "epoch": EPOCH,
        "opposite_windows": len(opposite),
        "aligned_windows": len(aligned),
        "variants": variants,
        "note": (
            "Derived nightly from the recorded wick book and the full-window "
            "quote log — no paper engine. Δ is what the live book would have "
            "gained (+) or lost (−) under each rule, fees charged on every "
            "added or exiting clip. P(Δ>0) is a day-clustered bootstrap; the "
            "2026-09-25 studies found every variant inside noise at 9 "
            "day-clusters, so treat nothing here as real before ~30 "
            "day-clusters hold a P(Δ>0) above 0.95."
        ),
    }
