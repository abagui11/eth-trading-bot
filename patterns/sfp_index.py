"""Deterministic SFP event index stored in ohlc.db."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import config
import ohlc_cache
from patterns.sfp import SFPEvent, detect_sfps

_SFP_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sfp_events (
    product_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts TEXT NOT NULL,
    direction TEXT NOT NULL,
    swept_level REAL NOT NULL,
    outcome_a TEXT NOT NULL,
    bar_idx INTEGER NOT NULL,
    built_at TEXT NOT NULL,
    PRIMARY KEY (product_id, timeframe, ts, direction)
);
CREATE INDEX IF NOT EXISTS idx_sfp_events_lookup
    ON sfp_events (product_id, timeframe, outcome_a, ts);
"""

_INDEX_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS sfp_index_meta (
    product_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    years INTEGER NOT NULL,
    candle_max_ts TEXT,
    event_count INTEGER NOT NULL,
    built_at TEXT NOT NULL,
    PRIMARY KEY (product_id, timeframe, years)
);
"""

_TF_BAR_LOADERS = {
    "W1": ohlc_cache.get_weekly_bars,
    "D1": ohlc_cache.get_daily_bars,
    "H12": ohlc_cache.get_h12_bars,
}


def _connect_ohlc() -> sqlite3.Connection:
    conn = sqlite3.connect(config.OHLC_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_index() -> None:
    ohlc_cache.init_cache()
    with _connect_ohlc() as conn:
        conn.executescript(_SFP_EVENTS_SCHEMA + _INDEX_META_SCHEMA)
        conn.commit()


def _load_bars(product_id: str, timeframe: str, years: int) -> list[dict[str, float | str]]:
    loader = _TF_BAR_LOADERS.get(timeframe.upper())
    if loader is None:
        raise ValueError(f"Unsupported index timeframe: {timeframe}")
    return loader(years=years, product_id=product_id)


def _candle_max_ts(product_id: str, timeframe: str) -> str | None:
    tf = timeframe.upper()
    if tf == "D1":
        _, max_ts, _ = ohlc_cache.cache_coverage(
            ohlc_cache.DAILY_GRANULARITY, product_id=product_id
        )
        return max_ts
    if tf == "W1":
        _, max_ts, _ = ohlc_cache.cache_coverage(
            ohlc_cache.DAILY_GRANULARITY, product_id=product_id
        )
        return max_ts
    if tf == "H12":
        _, max_ts, _ = ohlc_cache.cache_coverage(
            ohlc_cache.HOURLY_GRANULARITY, product_id=product_id
        )
        return max_ts
    return None


def rebuild_sfp_index(
    product_id: str,
    timeframe: str,
    years: int = 4,
) -> dict[str, Any]:
    """Detect SFPs and upsert into sfp_events. Returns rebuild summary."""
    init_index()
    tf = timeframe.upper()
    bars = _load_bars(product_id, tf, years)
    events = detect_sfps(bars, timeframe=tf) if bars else []
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    candle_max = _candle_max_ts(product_id, tf)

    with _connect_ohlc() as conn:
        conn.execute(
            """
            DELETE FROM sfp_events
            WHERE product_id = ? AND timeframe = ?
            """,
            (product_id, tf),
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO sfp_events
                (product_id, timeframe, ts, direction, swept_level,
                 outcome_a, bar_idx, built_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    product_id,
                    tf,
                    e.ts,
                    e.direction,
                    float(e.swept_level),
                    str(e.outcome_a),
                    int(e.bar_idx),
                    built_at,
                )
                for e in events
            ],
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO sfp_index_meta
                (product_id, timeframe, years, candle_max_ts, event_count, built_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (product_id, tf, years, candle_max, len(events), built_at),
        )
        conn.commit()

    return {
        "product_id": product_id,
        "timeframe": tf,
        "years": years,
        "event_count": len(events),
        "candle_max_ts": candle_max,
        "built_at": built_at,
    }


def is_index_stale(product_id: str, timeframe: str, years: int = 4) -> bool:
    """True when no meta row or candle max_ts advanced past last rebuild."""
    init_index()
    tf = timeframe.upper()
    with _connect_ohlc() as conn:
        row = conn.execute(
            """
            SELECT candle_max_ts, event_count FROM sfp_index_meta
            WHERE product_id = ? AND timeframe = ? AND years = ?
            """,
            (product_id, tf, years),
        ).fetchone()
    if row is None:
        return True
    current_max = _candle_max_ts(product_id, tf)
    if current_max and row["candle_max_ts"] and current_max > row["candle_max_ts"]:
        return True
    if current_max and not row["candle_max_ts"]:
        return True
    return False


def ensure_sfp_index(
    product_id: str,
    timeframe: str,
    years: int = 4,
) -> dict[str, Any]:
    """Rebuild when missing or stale; otherwise return meta summary."""
    init_index()
    tf = timeframe.upper()
    if is_index_stale(product_id, tf, years):
        return rebuild_sfp_index(product_id, tf, years)
    with _connect_ohlc() as conn:
        row = conn.execute(
            """
            SELECT product_id, timeframe, years, candle_max_ts, event_count, built_at
            FROM sfp_index_meta
            WHERE product_id = ? AND timeframe = ? AND years = ?
            """,
            (product_id, tf, years),
        ).fetchone()
    if row is None:
        return rebuild_sfp_index(product_id, tf, years)
    return dict(row)


def query_sfp_events(
    product_id: str,
    timeframe: str,
    *,
    outcome_a: str | None = None,
    start_ts: str | None = None,
    end_ts: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return indexed SFP rows ordered by ts ascending."""
    init_index()
    tf = timeframe.upper()
    query = """
        SELECT product_id, timeframe, ts, direction, swept_level, outcome_a, bar_idx, built_at
        FROM sfp_events
        WHERE product_id = ? AND timeframe = ?
    """
    params: list[Any] = [product_id, tf]
    if outcome_a:
        query += " AND outcome_a = ?"
        params.append(outcome_a)
    if start_ts:
        query += " AND ts >= ?"
        params.append(start_ts)
    if end_ts:
        query += " AND ts <= ?"
        params.append(end_ts)
    query += " ORDER BY ts ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))

    with _connect_ohlc() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def count_sfps(
    product_id: str,
    timeframe: str,
    *,
    years: int = 4,
    ensure: bool = True,
) -> int:
    """Count indexed SFPs for product/TF; optionally rebuild first."""
    if ensure:
        ensure_sfp_index(product_id, timeframe, years)
    init_index()
    tf = timeframe.upper()
    with _connect_ohlc() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM sfp_events
            WHERE product_id = ? AND timeframe = ?
            """,
            (product_id, tf),
        ).fetchone()
    return int(row["cnt"]) if row else 0


def list_invalidations(
    product_id: str,
    timeframe: str,
    *,
    years: int = 4,
    limit: int = 10,
    ensure: bool = True,
) -> list[dict[str, Any]]:
    """Most recent invalidation SFPs (newest last)."""
    if ensure:
        ensure_sfp_index(product_id, timeframe, years)
    events = query_sfp_events(product_id, timeframe, outcome_a="invalidation")
    if limit <= 0:
        return events
    return events[-limit:]


def events_as_sfp_events(rows: list[dict[str, Any]]) -> list[SFPEvent]:
    """Convert index rows to SFPEvent shells (outcomes B/C unset)."""
    out: list[SFPEvent] = []
    for row in rows:
        out.append(
            SFPEvent(
                ts=str(row["ts"]),
                bar_idx=int(row["bar_idx"]),
                timeframe=str(row["timeframe"]),
                direction=row["direction"],  # type: ignore[arg-type]
                swept_level=float(row["swept_level"]),
                sweep_depth_pct=0.0,
                aligns_prior_swing=False,
                volume_spike=False,
                outcome_a=row["outcome_a"],  # type: ignore[arg-type]
                outcome_b=None,
                outcome_c=None,
            )
        )
    return out
