"""Post-invalidation forward outcome scoring for SFP research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from patterns import config
from patterns.outcomes import _has_full_window
from patterns.sfp import SFPEvent

PostInvalidation = Literal["continuation", "mean_reversion", "neutral", "pending"]


@dataclass
class InvalidationFollowUp:
    event: SFPEvent
    invalidation_bar_idx: int | None
    outcome: PostInvalidation
    move_pct: float | None  # max move in the scored direction (percent)


def find_invalidation_bar(
    df: pd.DataFrame,
    event_idx: int,
    direction: Literal["bullish", "bearish"],
    swept_level: float,
    n_bars: int,
) -> int | None:
    """First bar after the SFP where close invalidates the swept level."""
    end = min(event_idx + 1 + n_bars, len(df))
    for i in range(event_idx + 1, end):
        close = float(df.iloc[i]["close"])
        if direction == "bullish" and close < swept_level:
            return i
        if direction == "bearish" and close > swept_level:
            return i
    return None


def score_post_invalidation(
    df: pd.DataFrame,
    event: SFPEvent,
    *,
    post_n_bars: int | None = None,
) -> InvalidationFollowUp:
    """
  Score price action after an invalidated SFP.

  - continuation: invalidation direction extends (>= min move from inv close)
  - mean_reversion: fades back toward the original SFP thesis
  - neutral: neither threshold within post window
  """
    tf = event.timeframe
    window_n = config.OUTCOME_N.get(tf, config.OUTCOME_N["H12"])
    post_n = post_n_bars if post_n_bars is not None else window_n
    min_move = config.REVERSAL_MIN_MOVE.get(tf, config.REVERSAL_MIN_MOVE["H12"])

    inv_bar = find_invalidation_bar(
        df,
        event.bar_idx,
        event.direction,
        event.swept_level,
        window_n,
    )
    if inv_bar is None:
        return InvalidationFollowUp(event, None, "pending", None)

    if not _has_full_window(inv_bar, post_n, len(df)):
        return InvalidationFollowUp(event, inv_bar, "pending", None)

    inv_close = float(df.iloc[inv_bar]["close"])
    window = df.iloc[inv_bar + 1 : inv_bar + 1 + post_n]
    max_high = float(window["high"].max())
    min_low = float(window["low"].min())
    up_move = (max_high - inv_close) / inv_close if inv_close else 0.0
    down_move = (inv_close - min_low) / inv_close if inv_close else 0.0

    if event.direction == "bearish":
        # Invalidated bearish SFP -> acceptance above level (bullish)
        if up_move >= min_move and up_move >= down_move:
            return InvalidationFollowUp(event, inv_bar, "continuation", round(up_move * 100, 2))
        if down_move >= min_move:
            return InvalidationFollowUp(event, inv_bar, "mean_reversion", round(down_move * 100, 2))
        return InvalidationFollowUp(
            event, inv_bar, "neutral", round(max(up_move, down_move) * 100, 2)
        )

    # Bullish SFP invalidated -> acceptance below level (bearish)
    if down_move >= min_move and down_move >= up_move:
        return InvalidationFollowUp(event, inv_bar, "continuation", round(down_move * 100, 2))
    if up_move >= min_move:
        return InvalidationFollowUp(event, inv_bar, "mean_reversion", round(up_move * 100, 2))
    return InvalidationFollowUp(
        event, inv_bar, "neutral", round(max(up_move, down_move) * 100, 2)
    )


def compute_invalidation_stats(followups: list[InvalidationFollowUp]) -> dict:
    continuation = sum(1 for f in followups if f.outcome == "continuation")
    mean_reversion = sum(1 for f in followups if f.outcome == "mean_reversion")
    neutral = sum(1 for f in followups if f.outcome == "neutral")
    pending = sum(1 for f in followups if f.outcome == "pending")
    scored = continuation + mean_reversion + neutral
    continuation_pct = (continuation / scored * 100) if scored else 0.0
    return {
        "total": len(followups),
        "continuation": continuation,
        "mean_reversion": mean_reversion,
        "neutral": neutral,
        "pending": pending,
        "continuation_pct": round(continuation_pct, 1),
        "scored": scored,
    }
