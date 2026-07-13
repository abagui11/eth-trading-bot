"""ICT-style order block detection from OHLC structure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from bot_config import (
    ADD_FIB_LEVEL,
    ENTRY_FIB_HIGH,
    ENTRY_FIB_LOW,
    FIB_LEVEL_TOLERANCE_PCT,
    OB_MIN_WIDTH_PCT,
)
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


def ob_width_pct(low: float, high: float) -> float:
    """Zone width as percent of mid price (same formula as 24h range)."""
    if high <= low:
        return 0.0
    mid = (high + low) / 2
    return (high - low) / mid * 100


def meets_min_ob_width(
    low: float,
    high: float,
    min_width_pct: float = OB_MIN_WIDTH_PCT,
) -> bool:
    """True when an OB zone is at least min_width_pct wide."""
    return ob_width_pct(low, high) >= min_width_pct


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
        low = round(float(row["low"]), 2)
        high = round(float(row["high"]), 2)
        if not meets_min_ob_width(low, high):
            return None
        ts = df.index[j].strftime("%Y-%m-%dT%H:%M:%SZ")
        disp_ts = df.index[displacement_idx].strftime("%Y-%m-%dT%H:%M:%SZ")
        return OrderBlock(
            direction=direction,
            low=low,
            high=high,
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


def fib_zone_bounds(
    direction: Direction,
    low: float,
    high: float,
    fib_low: float = ENTRY_FIB_LOW,
    fib_high: float = ENTRY_FIB_HIGH,
) -> tuple[float, float]:
    """Entry band inside an OB (default 0.25–0.50)."""
    span = high - low
    if span <= 0:
        return low, high
    if direction == "bearish":
        z0 = low + span * (1 - fib_high)
        z1 = low + span * (1 - fib_low)
    else:
        z0 = low + span * fib_low
        z1 = low + span * fib_high
    return round(min(z0, z1), 2), round(max(z0, z1), 2)


def fib_level(direction: Direction, low: float, high: float, fib: float) -> float:
    """Single fib mark inside an OB."""
    span = high - low
    if span <= 0:
        return low
    if direction == "bearish":
        return round(low + span * (1 - fib), 2)
    return round(low + span * fib, 2)


def price_in_fib_zone(
    price: float,
    direction: Direction,
    low: float,
    high: float,
    fib_low: float = ENTRY_FIB_LOW,
    fib_high: float = ENTRY_FIB_HIGH,
    tolerance_pct: float = FIB_LEVEL_TOLERANCE_PCT,
) -> bool:
    """True when price is inside the entry fib band (with tolerance)."""
    zone_low, zone_high = fib_zone_bounds(direction, low, high, fib_low, fib_high)
    pad = max(zone_high - zone_low, low * tolerance_pct, 1.0) * tolerance_pct
    return (zone_low - pad) <= price <= (zone_high + pad)


def near_fib_level(
    price: float,
    direction: Direction,
    low: float,
    high: float,
    fib: float,
    tolerance_pct: float = FIB_LEVEL_TOLERANCE_PCT,
) -> bool:
    """True when price is near a single fib mark (for staged entries / adds)."""
    level = fib_level(direction, low, high, fib)
    pad = max(abs(level) * tolerance_pct, 1.0)
    return abs(price - level) <= pad


def price_in_ob(
    price: float,
    ob: OrderBlock,
    fib_low: float = ENTRY_FIB_LOW,
    fib_high: float = ENTRY_FIB_HIGH,
) -> bool:
    """True when price sits in the OB entry fib band."""
    return price_in_fib_zone(price, ob.direction, ob.low, ob.high, fib_low, fib_high)


def price_in_full_ob(price: float, ob: OrderBlock) -> bool:
    """True when price is anywhere inside the OB bounds (for sweep-reversal context)."""
    return ob.low <= price <= ob.high


def entry_valid_at_price(
    price: float,
    direction: Direction,
    low: float,
    high: float,
    *,
    allow_add_level: bool = False,
) -> bool:
    """Entry or scale-in price is on a configured fib mark or inside the entry band."""
    if price_in_fib_zone(price, direction, low, high):
        return True
    if near_fib_level(price, direction, low, high, ENTRY_FIB_LOW):
        return True
    if near_fib_level(price, direction, low, high, ENTRY_FIB_HIGH):
        return True
    if allow_add_level and near_fib_level(price, direction, low, high, ADD_FIB_LEVEL):
        return True
    return False


def order_block_ref(ob: OrderBlock) -> str:
    """Stable key for tranche / scale-in tracking on a detected entry OB."""
    return f"{ob.direction}:{ob.displacement_ts}:{ob.low:.2f}:{ob.high:.2f}"


def zones_overlap(
    low_a: float,
    high_a: float,
    low_b: float,
    high_b: float,
    min_overlap_ratio: float = 0.5,
) -> bool:
    """True when two price zones share enough horizontal overlap."""
    span_a = high_a - low_a
    span_b = high_b - low_b
    if span_a <= 0 or span_b <= 0:
        return False
    overlap = max(0.0, min(high_a, high_b) - max(low_a, low_b))
    ref = min(span_a, span_b)
    return overlap / ref >= min_overlap_ratio


def bounds_close(
    low_a: float,
    high_a: float,
    low_b: float,
    high_b: float,
    tol_pct: float = 0.01,
) -> bool:
    """True when two OB bounds are effectively the same zone."""
    ref = max(high_b - low_b, 1.0)
    return abs(low_a - low_b) <= ref * tol_pct and abs(high_a - high_b) <= ref * tol_pct


def order_block_dict_matches(
    ob_dict: dict,
    candidate: OrderBlock,
    *,
    min_overlap_ratio: float = 0.5,
) -> bool:
    """True when JSON order_block aligns with a detected entry OrderBlock."""
    del min_overlap_ratio
    try:
        low = float(ob_dict["low"])
        high = float(ob_dict["high"])
    except (KeyError, TypeError, ValueError):
        return False
    if ob_dict.get("start_ts") and ob_dict["start_ts"] == candidate.start_ts:
        return True
    return bounds_close(low, high, candidate.low, candidate.high)


def find_matching_entry_ob(
    ob_dict: dict,
    order_blocks: list[OrderBlock],
    direction: Direction,
) -> OrderBlock | None:
    """Best matching detected entry OB for a trade's order_block field."""
    matches = [
        ob
        for ob in order_blocks
        if ob.direction == direction and order_block_dict_matches(ob_dict, ob)
    ]
    if not matches:
        return None
    return max(matches, key=lambda ob: ob.displacement_ts)


def find_matching_h1_ob(
    ob_dict: dict,
    order_blocks: list[OrderBlock],
    direction: Direction,
) -> OrderBlock | None:
    """Alias for ``find_matching_entry_ob`` (historical name)."""
    return find_matching_entry_ob(ob_dict, order_blocks, direction)


def format_ob_with_fib(ob: OrderBlock) -> str:
    """Single-line summary with fib entry band for prompts."""
    z_low, z_high = fib_zone_bounds(ob.direction, ob.low, ob.high)
    t1 = fib_level(ob.direction, ob.low, ob.high, ENTRY_FIB_LOW)
    t2 = fib_level(ob.direction, ob.low, ob.high, ENTRY_FIB_HIGH)
    add = fib_level(ob.direction, ob.low, ob.high, ADD_FIB_LEVEL)
    return (
        f"{ob.direction} M5 OB {ob.low:,.2f}-{ob.high:,.2f} "
        f"(candle {ob.start_ts[:16]}, displacement {ob.displacement_ts[:16]}) "
        f"| entry band 0.25-0.50: {z_low:,.2f}-{z_high:,.2f} "
        f"| tranches @ {t1:,.2f} / {t2:,.2f} | add @ {add:,.2f}"
    )
