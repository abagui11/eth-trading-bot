"""HTF market structure: order blocks and breakers (IMG-style rules).

Live agent feeds H4 bars; research may still use H12.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from patterns.order_block import meets_min_ob_width
from patterns.swing import Pivot, find_pivots

ZoneType = Literal["order_block", "breaker"]
Direction = Literal["bullish", "bearish"]


@dataclass
class HTFZone:
    zone_type: ZoneType
    direction: Direction
    low: float
    high: float
    start_ts: str
    end_ts: str | None = None
    mitigated: bool = False
    msb_ts: str = ""

    @property
    def formed_ts(self) -> str:
        """Backward-compatible alias for chart code expecting formed_ts."""
        return self.start_ts


@dataclass
class _TrackedBlock:
    zone_type: ZoneType
    direction: Direction
    low: float
    high: float
    start_ts: str
    msb_ts: str
    start_idx: int
    mitigated: bool = False
    end_ts: str | None = None
    promoted_to_breaker: bool = False


def _last_swing_before(pivots: list[Pivot], idx: int, kind: Literal["high", "low"]) -> Pivot | None:
    candidates = [p for p in pivots if p.kind == kind and p.idx < idx]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.idx)


def _candle_direction(row: pd.Series) -> Direction:
    return "bullish" if float(row["close"]) >= float(row["open"]) else "bearish"


def _ts_at(df: pd.DataFrame, idx: int) -> str:
    return df.index[idx].strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_ob_candidate(
    df: pd.DataFrame,
    msb_idx: int,
    direction: Direction,
    broken_level: float,
) -> tuple[int, float, float, str] | None:
    """Last opposite-direction candle immediately before MSB."""
    del broken_level
    opposite: Direction = "bearish" if direction == "bullish" else "bullish"
    for j in range(msb_idx - 1, max(msb_idx - 30, -1), -1):
        row = df.iloc[j]
        if _candle_direction(row) != opposite:
            continue
        low = round(float(row["low"]), 2)
        high = round(float(row["high"]), 2)
        if not meets_min_ob_width(low, high):
            return None
        return (j, low, high, _ts_at(df, j))
    return None


def _close_mitigates(direction: Direction, close: float, low: float, high: float) -> bool:
    if direction == "bullish":
        return close < low
    return close > high


def _to_zone(block: _TrackedBlock) -> HTFZone:
    return HTFZone(
        zone_type=block.zone_type,
        direction=block.direction,
        low=block.low,
        high=block.high,
        start_ts=block.start_ts,
        end_ts=block.end_ts,
        mitigated=block.mitigated,
        msb_ts=block.msb_ts,
    )


def detect_htf_zones(
    htf_bars: list[dict],
    lookback: int = 60,
    pivot_left: int = 2,
    pivot_right: int = 2,
) -> list[HTFZone]:
    """
    Detect HTF order blocks and breakers on closed candles.

    MSB: close through prior swing high/low (wick-only breaks ignored).
    OB: last opposite candle before MSB.
    Breaker: mitigated OB followed by opposite MSB.
    """
    if len(htf_bars) < 15:
        return []

    df = pd.DataFrame(htf_bars)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float}
    )

    pivots = find_pivots(df, left=pivot_left, right=pivot_right)
    start_idx = max(0, len(df) - lookback)

    active_obs: list[_TrackedBlock] = []
    violated_obs: list[_TrackedBlock] = []
    all_blocks: list[_TrackedBlock] = []

    for i in range(start_idx, len(df)):
        row = df.iloc[i]
        close = float(row["close"])
        ts = _ts_at(df, i)

        for block in active_obs:
            if block.mitigated:
                continue
            if _close_mitigates(block.direction, close, block.low, block.high):
                block.mitigated = True
                block.end_ts = ts
                violated_obs.append(block)

        swing_high = _last_swing_before(pivots, i, "high")
        swing_low = _last_swing_before(pivots, i, "low")

        if swing_high and close > swing_high.price:
            for v in reversed(violated_obs):
                if v.promoted_to_breaker or v.direction != "bearish":
                    continue
                breaker = _TrackedBlock(
                    zone_type="breaker",
                    direction="bullish",
                    low=v.low,
                    high=v.high,
                    start_ts=v.start_ts,
                    msb_ts=ts,
                    start_idx=v.start_idx,
                )
                all_blocks.append(breaker)
                v.promoted_to_breaker = True
                break

            ob = _find_ob_candidate(df, i, "bullish", swing_high.price)
            if ob:
                idx, low, high, start_ts = ob
                block = _TrackedBlock(
                    zone_type="order_block",
                    direction="bullish",
                    low=round(low, 2),
                    high=round(high, 2),
                    start_ts=start_ts,
                    msb_ts=ts,
                    start_idx=idx,
                )
                active_obs.append(block)
                all_blocks.append(block)

        if swing_low and close < swing_low.price:
            for v in reversed(violated_obs):
                if v.promoted_to_breaker or v.direction != "bullish":
                    continue
                breaker = _TrackedBlock(
                    zone_type="breaker",
                    direction="bearish",
                    low=v.low,
                    high=v.high,
                    start_ts=v.start_ts,
                    msb_ts=ts,
                    start_idx=v.start_idx,
                )
                all_blocks.append(breaker)
                v.promoted_to_breaker = True
                break

            ob = _find_ob_candidate(df, i, "bearish", swing_low.price)
            if ob:
                idx, low, high, start_ts = ob
                block = _TrackedBlock(
                    zone_type="order_block",
                    direction="bearish",
                    low=round(low, 2),
                    high=round(high, 2),
                    start_ts=start_ts,
                    msb_ts=ts,
                    start_idx=idx,
                )
                active_obs.append(block)
                all_blocks.append(block)

    seen: set[str] = set()
    unique: list[HTFZone] = []
    for block in all_blocks:
        # Once promoted, the breaker replaces the OB — do not export both.
        if block.zone_type == "order_block" and block.promoted_to_breaker:
            continue
        key = f"{block.zone_type}:{block.direction}:{block.low}:{block.high}:{block.start_ts}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(_to_zone(block))
    return unique[-12:]
