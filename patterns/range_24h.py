"""24-hour range detection on H1 bars."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

RangeBreak = Literal["above", "below"]


@dataclass
class Range24h:
    high: float
    low: float
    mid: float
    width_pct: float
    is_ranging: bool
    bars_in_range: int
    start_ts: str
    end_ts: str


def compute_range_24h(bars: list[dict], window: int = 24) -> Range24h | None:
    """High/low of the last `window` H1 candles plus ranging heuristic."""
    if len(bars) < window:
        return None

    df = pd.DataFrame(bars[-window:])
    high = float(df["high"].max())
    low = float(df["low"].min())
    if high <= low:
        return None

    mid = (high + low) / 2
    width_pct = (high - low) / mid * 100
    closes = df["close"].astype(float)
    bars_in_range = int(((closes >= low) & (closes <= high)).sum())
    last_close = float(closes.iloc[-1])
    is_ranging = low <= last_close <= high and bars_in_range >= max(window - 4, window * 2 // 3)

    return Range24h(
        high=round(high, 2),
        low=round(low, 2),
        mid=round(mid, 2),
        width_pct=round(width_pct, 2),
        is_ranging=is_ranging,
        bars_in_range=bars_in_range,
        start_ts=str(df.iloc[0]["ts"]),
        end_ts=str(df.iloc[-1]["ts"]),
    )


def detect_range_break(
    close: float,
    prev_high: float,
    prev_low: float,
) -> RangeBreak | None:
    """Return break direction when `close` clears the prior announced range."""
    if close > prev_high:
        return "above"
    if close < prev_low:
        return "below"
    return None
