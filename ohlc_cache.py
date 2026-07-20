"""SQLite cache for historical OHLC candles (research / backfill)."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import config
import research

PRODUCT_ID = research.PRODUCT_ID  # default ETH-USD
DAILY_GRANULARITY = "ONE_DAY"
HOURLY_GRANULARITY = "ONE_HOUR"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    product_id TEXT NOT NULL,
    granularity TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (product_id, granularity, ts)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.OHLC_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_cache() -> None:
    with _connect() as conn:
        conn.execute(_SCHEMA)
        conn.commit()


def upsert_candles(
    granularity: str,
    bars: list[dict],
    *,
    product_id: str = PRODUCT_ID,
) -> int:
    """Insert or replace candles. Returns number of rows written."""
    if not bars:
        return 0
    init_cache()
    with _connect() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO candles
                (product_id, granularity, ts, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    product_id,
                    granularity,
                    str(bar["ts"]),
                    float(bar["open"]),
                    float(bar["high"]),
                    float(bar["low"]),
                    float(bar["close"]),
                    float(bar["volume"]),
                )
                for bar in bars
            ],
        )
        conn.commit()
    return len(bars)


def get_candles(
    granularity: str,
    start_ts: str | None = None,
    end_ts: str | None = None,
    *,
    product_id: str = PRODUCT_ID,
) -> list[dict[str, float | str]]:
    """Return sorted candles from cache, optionally filtered by ISO ts bounds."""
    init_cache()
    query = """
        SELECT ts, open, high, low, close, volume
        FROM candles
        WHERE product_id = ? AND granularity = ?
    """
    params: list = [product_id, granularity]
    if start_ts:
        query += " AND ts >= ?"
        params.append(start_ts)
    if end_ts:
        query += " AND ts <= ?"
        params.append(end_ts)
    query += " ORDER BY ts ASC"

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        {
            "ts": row["ts"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        for row in rows
    ]


def cache_coverage(
    granularity: str,
    *,
    product_id: str = PRODUCT_ID,
) -> tuple[str | None, str | None, int]:
    """Return (min_ts, max_ts, count) for cached granularity."""
    init_cache()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT MIN(ts) AS min_ts, MAX(ts) AS max_ts, COUNT(*) AS cnt
            FROM candles
            WHERE product_id = ? AND granularity = ?
            """,
            (product_id, granularity),
        ).fetchone()
    if row is None or row["cnt"] == 0:
        return None, None, 0
    return row["min_ts"], row["max_ts"], int(row["cnt"])


def backfill_daily(years: int = 4, *, product_id: str = PRODUCT_ID) -> dict:
    """Fetch and cache daily candles for the past `years` years."""
    init_cache()
    end = int(time.time())
    start = int((datetime.now(timezone.utc) - timedelta(days=365 * years)).timestamp())

    bars = research.fetch_coinbase_candles_range(
        DAILY_GRANULARITY, start, end, product_id=product_id
    )
    written = upsert_candles(DAILY_GRANULARITY, bars, product_id=product_id)
    min_ts, max_ts, count = cache_coverage(DAILY_GRANULARITY, product_id=product_id)
    return {
        "product_id": product_id,
        "granularity": DAILY_GRANULARITY,
        "fetched": len(bars),
        "written": written,
        "count": count,
        "min_ts": min_ts,
        "max_ts": max_ts,
    }


def ensure_daily_history(
    years: int = 4,
    *,
    product_id: str = PRODUCT_ID,
) -> list[dict[str, float | str]]:
    """Return daily bars for the past `years` years, backfilling cache if needed."""
    min_needed = (
        datetime.now(timezone.utc) - timedelta(days=365 * years)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    min_ts, max_ts, count = cache_coverage(DAILY_GRANULARITY, product_id=product_id)
    if count == 0 or (min_ts and min_ts > min_needed):
        backfill_daily(years=years, product_id=product_id)

    return get_candles(DAILY_GRANULARITY, start_ts=min_needed, product_id=product_id)


def backfill_hourly(years: int = 4, *, product_id: str = PRODUCT_ID) -> dict:
    """Fetch and cache H1 candles for the past `years` years."""
    init_cache()
    end = int(time.time())
    start = int((datetime.now(timezone.utc) - timedelta(days=365 * years)).timestamp())

    bars = research.fetch_coinbase_candles_range(
        HOURLY_GRANULARITY, start, end, product_id=product_id
    )
    written = upsert_candles(HOURLY_GRANULARITY, bars, product_id=product_id)
    min_ts, max_ts, count = cache_coverage(HOURLY_GRANULARITY, product_id=product_id)
    return {
        "product_id": product_id,
        "granularity": HOURLY_GRANULARITY,
        "fetched": len(bars),
        "written": written,
        "count": count,
        "min_ts": min_ts,
        "max_ts": max_ts,
    }


def ensure_hourly_history(
    years: int = 4,
    *,
    product_id: str = PRODUCT_ID,
) -> list[dict[str, float | str]]:
    """Return H1 bars for the past `years` years, backfilling cache if needed."""
    min_needed = (
        datetime.now(timezone.utc) - timedelta(days=365 * years)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    min_ts, max_ts, count = cache_coverage(HOURLY_GRANULARITY, product_id=product_id)
    if count == 0 or (min_ts and min_ts > min_needed):
        backfill_hourly(years=years, product_id=product_id)

    return get_candles(HOURLY_GRANULARITY, start_ts=min_needed, product_id=product_id)


def get_daily_bars(
    years: int = 4,
    *,
    product_id: str = PRODUCT_ID,
) -> list[dict[str, float | str]]:
    """Raw daily bars from cache (not resampled)."""
    return ensure_daily_history(years=years, product_id=product_id)


def get_h12_bars(
    years: int = 4,
    *,
    product_id: str = PRODUCT_ID,
) -> list[dict[str, float | str]]:
    """H1 cache resampled to 12-hour bars."""
    hourly = ensure_hourly_history(years=years, product_id=product_id)
    if not hourly:
        return []
    # ~2 twelve-hour bars per calendar day.
    return research._resample_h12(hourly, limit=years * 365 * 2 + 8)


def get_weekly_bars(
    years: int = 4,
    *,
    product_id: str = PRODUCT_ID,
) -> list[dict[str, float | str]]:
    """Daily cache resampled to W-FRI weekly bars."""
    daily = ensure_daily_history(years=years, product_id=product_id)
    if not daily:
        return []
    # ~52 weeks per year + buffer
    return research._resample_weekly(daily, limit=52 * years + 4)
