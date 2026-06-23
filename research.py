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
    # W1: fetch max daily window, resample to weekly, return last 52 weeks.
    "W1": {
        "granularity": "ONE_DAY",
        "seconds": 86400,
        "limit": 52,
        "daily_fetch_limit": _MAX_CANDLES,
        "resample_weekly": True,
    },
}

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


def _fetch_coinbase_candles(granularity: str, limit: int) -> list[dict[str, float | str]]:
    seconds = _GRANULARITY_SECONDS[granularity]
    capped = min(limit, _MAX_CANDLES)
    end = int(time.time())
    start = end - capped * seconds

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
        raise RuntimeError(f"No candles returned for {PRODUCT_ID} {granularity}")

    bars = [_normalize_candle(c) for c in candles]
    bars.sort(key=lambda b: b["ts"])
    return bars[-capped:]


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


def get_ohlc(timeframe: str, limit: int | None = None) -> list[dict[str, float | str]]:
    """Pull ETH candles for a supported timeframe (W1, D1, H4, H1)."""
    tf = timeframe.upper()
    if tf not in _TIMEFRAME_CONFIG:
        raise ValueError(f"Unsupported timeframe: {timeframe}. Use one of {list(_TIMEFRAME_CONFIG)}")

    cfg = _TIMEFRAME_CONFIG[tf]
    bar_limit = limit if limit is not None else cfg["limit"]
    granularity = cfg["granularity"]

    if cfg.get("resample_weekly"):
        fetch_limit = cfg.get("daily_fetch_limit", _MAX_CANDLES)
    else:
        fetch_limit = bar_limit
    bars = _fetch_coinbase_candles(granularity, fetch_limit)

    if cfg.get("resample_weekly"):
        return _resample_weekly(bars, bar_limit)

    return bars


def get_all_timeframes() -> dict[str, list[dict[str, float | str]]]:
    """Fetch OHLC for all strategy timeframes."""
    return {tf: get_ohlc(tf) for tf in ("W1", "D1", "H4", "H1")}


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
