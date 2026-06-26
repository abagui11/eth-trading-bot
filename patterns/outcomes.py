"""Outcome scoring for SFP events (A, B, C)."""

from __future__ import annotations

from typing import Literal

import pandas as pd

from patterns import config
from patterns.swing import Pivot, find_pivots

OutcomeA = Literal["reversal", "invalidation", "pending", "neutral"]


def _has_full_window(event_idx: int, n_bars: int, df_len: int) -> bool:
    """True when N forward bars exist after the event bar to score outcomes."""
    return event_idx + 1 + n_bars <= df_len


def score_outcome_a(
    df: pd.DataFrame,
    event_idx: int,
    direction: Literal["bullish", "bearish"],
    swept_level: float,
    n_bars: int,
    timeframe: str = "W1",
) -> OutcomeA:
    """
    Outcome A: invalidation if close past swept level first;
    reversal if price reaches a minimum follow-through move (from event close)
    in the SFP direction without prior invalidation; neutral if neither within N bars.
    """
    if not _has_full_window(event_idx, n_bars, len(df)):
        return "pending"

    event_close = float(df.iloc[event_idx]["close"])
    min_move = config.REVERSAL_MIN_MOVE.get(timeframe, config.REVERSAL_MIN_MOVE["W1"])
    start = event_idx + 1
    end = event_idx + 1 + n_bars

    for i in range(start, end):
        close = float(df.iloc[i]["close"])
        high = float(df.iloc[i]["high"])
        low = float(df.iloc[i]["low"])
        if direction == "bullish":
            if close < swept_level:
                return "invalidation"
            if high >= event_close * (1 + min_move):
                return "reversal"
        else:
            if close > swept_level:
                return "invalidation"
            if low <= event_close * (1 - min_move):
                return "reversal"

    return "neutral"


def score_outcome_b(
    df: pd.DataFrame,
    event_idx: int,
    direction: Literal["bullish", "bearish"],
    n_bars: int,
    move_pct: float | None = None,
) -> bool | None:
    """Did price move >= move_pct in SFP direction within N bars? None = pending."""
    if not _has_full_window(event_idx, n_bars, len(df)):
        return None

    threshold = move_pct if move_pct is not None else config.MOVE_PCT_B
    ref_close = float(df.iloc[event_idx]["close"])
    window = df.iloc[event_idx + 1 : event_idx + 1 + n_bars]

    if direction == "bullish":
        max_high = float(window["high"].max())
        move = (max_high - ref_close) / ref_close
    else:
        min_low = float(window["low"].min())
        move = (ref_close - min_low) / ref_close

    return move >= threshold


def _last_swing_before(pivots: list[Pivot], event_idx: int, kind: Literal["high", "low"]) -> Pivot | None:
    candidates = [p for p in pivots if p.kind == kind and p.idx < event_idx]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.idx)


def score_outcome_c(
    df: pd.DataFrame,
    event_idx: int,
    direction: Literal["bullish", "bearish"],
    n_bars: int,
    pivots: list[Pivot] | None = None,
) -> bool | None:
    """Structure break in SFP direction within N bars. None = pending."""
    if not _has_full_window(event_idx, n_bars, len(df)):
        return None

    if pivots is None:
        pivots = find_pivots(df)
    # Only pivots confirmed before the event bar — no look-ahead.
    known_pivots = [p for p in pivots if p.idx < event_idx]

    window = df.iloc[event_idx + 1 : event_idx + 1 + n_bars]

    if direction == "bullish":
        prior = _last_swing_before(known_pivots, event_idx, "high")
        if prior is None:
            return False
        return float(window["high"].max()) > prior.price
    prior = _last_swing_before(known_pivots, event_idx, "low")
    if prior is None:
        return False
    return float(window["low"].min()) < prior.price
