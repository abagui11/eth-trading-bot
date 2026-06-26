"""Fetch ETH-USD OHLC candles from Coinbase public market data."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

import config

PRODUCT_ID = "ETH-USD"
_MAX_CANDLES = 350  # Coinbase hard cap per request

# Spec timeframes -> Coinbase granularity and default bar counts.
_TIMEFRAME_CONFIG: dict[str, dict[str, Any]] = {
    "H1": {"granularity": "ONE_HOUR", "seconds": 3600, "limit": 120},
    "H4": {"granularity": "FOUR_HOUR", "seconds": 14400, "limit": 90},
    "D1": {"granularity": "ONE_DAY", "seconds": 86400, "limit": 90},
    # H12: resample from paginated H1 fetch (Coinbase has no native 12h candles).
    "H12": {
        "granularity": "ONE_HOUR",
        "seconds": 3600,
        "limit": 90,
        "h1_fetch_bars": 90 * 12 + 12,
        "resample_h12": True,
    },
    # W1: fetch max daily window, resample to weekly, return last 52 weeks.
    "W1": {
        "granularity": "ONE_DAY",
        "seconds": 86400,
        "limit": 52,
        "daily_fetch_limit": _MAX_CANDLES,
        "resample_weekly": True,
    },
}

STRATEGY_TIMEFRAMES = ("H12", "H4", "H1")

_GRANULARITY_SECONDS = {
    "ONE_HOUR": 3600,
    "FOUR_HOUR": 14400,
    "ONE_DAY": 86400,
}


def _utc_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_candle(raw: dict[str, str]) -> dict[str, float | str]:
    return {
        "ts": _utc_iso(int(raw["start"])),
        "open": float(raw["open"]),
        "high": float(raw["high"]),
        "low": float(raw["low"]),
        "close": float(raw["close"]),
        "volume": float(raw["volume"]),
    }


def _fetch_coinbase_candles_page(
    granularity: str,
    start: int,
    end: int,
    limit: int,
) -> list[dict[str, float | str]]:
    """Fetch one page of candles for [start, end] (unix seconds)."""
    capped = min(limit, _MAX_CANDLES)
    url = f"{config.MARKET_DATA_API}/products/{PRODUCT_ID}/candles"
    params = {
        "start": str(start),
        "end": str(end),
        "granularity": granularity,
        "limit": capped,
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()

    candles = payload.get("candles", [])
    if not candles:
        return []

    bars = [_normalize_candle(c) for c in candles]
    bars.sort(key=lambda b: b["ts"])
    return bars


def _fetch_coinbase_candles(granularity: str, limit: int) -> list[dict[str, float | str]]:
    seconds = _GRANULARITY_SECONDS[granularity]
    capped = min(limit, _MAX_CANDLES)
    end = int(time.time())
    start = end - capped * seconds

    bars = _fetch_coinbase_candles_page(granularity, start, end, capped)
    if not bars:
        raise RuntimeError(f"No candles returned for {PRODUCT_ID} {granularity}")
    return bars[-capped:]


def fetch_coinbase_candles_range(
    granularity: str,
    start_ts: int,
    end_ts: int,
) -> list[dict[str, float | str]]:
    """Paginate Coinbase candles between unix start/end (inclusive window)."""
    if start_ts >= end_ts:
        raise ValueError("start_ts must be before end_ts")

    seconds = _GRANULARITY_SECONDS[granularity]
    window_seconds = _MAX_CANDLES * seconds
    all_bars: dict[str, dict[str, float | str]] = {}
    cursor = start_ts

    while cursor < end_ts:
        chunk_end = min(cursor + window_seconds, end_ts)
        page = _fetch_coinbase_candles_page(
            granularity, cursor, chunk_end, _MAX_CANDLES
        )
        if not page:
            cursor = chunk_end
            continue
        for bar in page:
            all_bars[str(bar["ts"])] = bar
        last_dt = datetime.fromisoformat(str(page[-1]["ts"]).replace("Z", "+00:00"))
        next_cursor = int(last_dt.timestamp()) + seconds
        if next_cursor <= cursor:
            cursor = chunk_end
        else:
            cursor = next_cursor

    return sorted(all_bars.values(), key=lambda b: b["ts"])


def _resample_h12(h1_bars: list[dict[str, float | str]], limit: int) -> list[dict[str, float | str]]:
    """Aggregate H1 candles into 12-hour bars."""
    df = pd.DataFrame(h1_bars)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts")

    h12 = df.resample("12h", origin="start").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna()

    h12 = h12.tail(limit)
    rows: list[dict[str, float | str]] = []
    for ts, row in h12.iterrows():
        rows.append(
            {
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )
    return rows


def _resample_weekly(daily_bars: list[dict[str, float | str]], limit: int) -> list[dict[str, float | str]]:
    df = pd.DataFrame(daily_bars)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts")

    weekly = df.resample("W-FRI").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna()

    weekly = weekly.tail(limit)
    rows: list[dict[str, float | str]] = []
    for ts, row in weekly.iterrows():
        rows.append(
            {
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )
    return rows


def fetch_h1_bars(count: int) -> list[dict[str, float | str]]:
    """Fetch `count` most recent H1 bars, paginating past the 350-candle API cap."""
    if count <= _MAX_CANDLES:
        return _fetch_coinbase_candles("ONE_HOUR", count)

    end = int(time.time())
    seconds = _GRANULARITY_SECONDS["ONE_HOUR"]
    start = end - count * seconds
    bars = fetch_coinbase_candles_range("ONE_HOUR", start, end)
    if not bars:
        raise RuntimeError(f"No H1 candles returned for {PRODUCT_ID}")
    return bars[-count:]


def get_ohlc(timeframe: str, limit: int | None = None) -> list[dict[str, float | str]]:
    """Pull ETH candles for a supported timeframe (H12, H4, H1, D1, W1)."""
    tf = timeframe.upper()
    if tf not in _TIMEFRAME_CONFIG:
        raise ValueError(f"Unsupported timeframe: {timeframe}. Use one of {list(_TIMEFRAME_CONFIG)}")

    cfg = _TIMEFRAME_CONFIG[tf]
    bar_limit = limit if limit is not None else cfg["limit"]
    granularity = cfg["granularity"]

    if cfg.get("resample_weekly"):
        fetch_limit = cfg.get("daily_fetch_limit", _MAX_CANDLES)
        bars = _fetch_coinbase_candles(granularity, fetch_limit)
        return _resample_weekly(bars, bar_limit)

    if cfg.get("resample_h12"):
        h1_count = cfg.get("h1_fetch_bars", bar_limit * 12 + 12)
        if limit is not None:
            h1_count = max(h1_count, bar_limit * 12 + 12)
        h1_bars = fetch_h1_bars(h1_count)
        return _resample_h12(h1_bars, bar_limit)

    bars = _fetch_coinbase_candles(granularity, bar_limit)
    return bars


def get_all_timeframes() -> dict[str, list[dict[str, float | str]]]:
    """Fetch OHLC for live strategy timeframes (H12, H4, H1)."""
    h1 = get_ohlc("H1")
    h4 = get_ohlc("H4")
    h1_for_h12 = fetch_h1_bars(_TIMEFRAME_CONFIG["H12"]["h1_fetch_bars"])
    h12 = _resample_h12(h1_for_h12, _TIMEFRAME_CONFIG["H12"]["limit"])
    return {"H12": h12, "H4": h4, "H1": h1}


def to_dataframe(bars: list[dict[str, float | str]]) -> pd.DataFrame:
    """Convert normalized bars to a datetime-indexed OHLCV DataFrame."""
    df = pd.DataFrame(bars)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float}
    )


def get_spot_price() -> float:
    """Latest ETH price from the most recent H1 close."""
    h1 = get_ohlc("H1", limit=1)
    return float(h1[-1]["close"])


if __name__ == "__main__":
    data = get_all_timeframes()
    summary = {
        tf: {
            "count": len(bars),
            "first": bars[0] if bars else None,
            "last": bars[-1] if bars else None,
        }
        for tf, bars in data.items()
    }
    summary["spot_price"] = get_spot_price()
    print(json.dumps(summary, indent=2))
