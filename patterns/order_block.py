"""ICT-style order block detection from OHLC structure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from patterns.swing import Pivot, find_pivots

Direction = Literal["bullish", "bearish"]


@dataclass
class OrderBlock:
    direction: Direction
    low: float
    high: float
    start_ts: str
    end_ts: str
    displacement_ts: str


def _last_swing_before(pivots: list[Pivot], idx: int, kind: Literal["high", "low"]) -> Pivot | None:
    candidates = [p for p in pivots if p.kind == kind and p.idx < idx]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.idx)


def _candle_direction(row: pd.Series) -> Direction:
    return "bullish" if float(row["close"]) >= float(row["open"]) else "bearish"


def ob_from_displacement(
    df: pd.DataFrame,
    displacement_idx: int,
    direction: Direction,
) -> OrderBlock | None:
    """Last opposite candle before a displacement bar that breaks structure."""
    return _ob_from_displacement(df, displacement_idx, direction)


def _ob_from_displacement(
    df: pd.DataFrame,
    displacement_idx: int,
    direction: Direction,
) -> OrderBlock | None:
    """Last opposite candle before a displacement bar that breaks structure."""
    opposite: Direction = "bearish" if direction == "bullish" else "bullish"
    for j in range(displacement_idx - 1, max(displacement_idx - 30, -1), -1):
        row = df.iloc[j]
        if _candle_direction(row) != opposite:
            continue
        ts = df.index[j].strftime("%Y-%m-%dT%H:%M:%SZ")
        disp_ts = df.index[displacement_idx].strftime("%Y-%m-%dT%H:%M:%SZ")
        return OrderBlock(
            direction=direction,
            low=round(float(row["low"]), 2),
            high=round(float(row["high"]), 2),
            start_ts=ts,
            end_ts=ts,
            displacement_ts=disp_ts,
        )
    return None


def find_order_blocks(bars: list[dict], lookback: int = 60) -> list[OrderBlock]:
    """
    Scan recent bars for displacement through swing structure.
    Bullish OB: last down candle before close breaks above prior swing high.
    Bearish OB: last up candle before close breaks below prior swing low.
    """
    if len(bars) < lookback:
        return []

    df = pd.DataFrame(bars)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float}
    )

    pivots = find_pivots(df)
    blocks: list[OrderBlock] = []
    start_idx = max(0, len(df) - lookback)

    for i in range(start_idx, len(df)):
        close = float(df.iloc[i]["close"])
        swing_high = _last_swing_before(pivots, i, "high")
        swing_low = _last_swing_before(pivots, i, "low")

        if swing_high and close > swing_high.price:
            ob = ob_from_displacement(df, i, "bullish")
            if ob:
                blocks.append(ob)
        if swing_low and close < swing_low.price:
            ob = ob_from_displacement(df, i, "bearish")
            if ob:
                blocks.append(ob)

    # Keep most recent unique zones (by displacement time).
    seen: set[str] = set()
    unique: list[OrderBlock] = []
    for ob in reversed(blocks):
        key = f"{ob.direction}:{ob.displacement_ts}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(ob)
    unique.reverse()
    return unique[-5:]


def price_in_ob(price: float, ob: OrderBlock, fib_low: float = 0.618, fib_high: float = 0.786) -> bool:
    """True when price sits in the OB discount/premium zone (fib slice of the block)."""
    span = ob.high - ob.low
    if span <= 0:
        return False
    zone_low = ob.low + span * (1 - fib_high) if ob.direction == "bearish" else ob.low + span * fib_low
    zone_high = ob.low + span * (1 - fib_low) if ob.direction == "bearish" else ob.low + span * fib_high
    if zone_low > zone_high:
        zone_low, zone_high = zone_high, zone_low
    return zone_low <= price <= zone_high
