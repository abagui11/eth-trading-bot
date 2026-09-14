"""BOS / CHoCH — causal market-structure breaks from confirmed pivots.

Break of Structure is continuation: price closes through the last swing in the
direction it was already trending. Change of Character is the reversal: price
closes through the swing on the *opposite* side of the prevailing trend, which
is the first mechanical evidence the trend has turned.

**Why this is not `smartmoneyconcepts.bos_choch()`.** That version is built on
``swing_highs_lows()``, which uses a forward-looking rolling window *and* a
``while True`` loop that retroactively deletes swings it previously flagged —
so a swing present at bar 100 can vanish once bar 140 arrives. It then writes
the break flag back onto ``bos[last_positions[-2]]``, an earlier bar than the
one that broke. Both make it unusable for a live trigger: signals flicker and
are dated before they were knowable.

Here a pivot is only visible once its right-hand confirmation bars have
closed, a break is attributed to the bar whose close breaches the level, and
each level fires at most once.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from patterns import config
from patterns.swing import find_pivots


@dataclass
class StructureBreak:
    idx: int          # bar whose close broke the level
    ts: str
    kind: str         # "bos" | "choch"
    direction: str    # "bullish" | "bearish"
    level: float      # the swing price that was broken
    pivot_idx: int    # bar the broken swing formed on


def detect_structure_breaks(
    df: pd.DataFrame,
    *,
    left: int | None = None,
    right: int | None = None,
    use_close: bool = True,
) -> list[StructureBreak]:
    """Walk bars forward, firing BOS/CHoCH as levels break. Oldest first.

    ``use_close`` requires a closing breach (the stricter, ICT-conventional
    reading). With it False a wick through the level is enough, which fires
    earlier and far more often on low timeframes.
    """
    l = left if left is not None else config.PIVOT_LEFT
    r = right if right is not None else config.PIVOT_RIGHT
    n = len(df)
    if n < l + r + 2:
        return []

    pivots = find_pivots(df, left=l, right=r)
    if not pivots:
        return []

    # A pivot at idx p is only knowable once bar p + r has closed.
    by_confirm: dict[int, list] = {}
    for p in pivots:
        by_confirm.setdefault(p.idx + r, []).append(p)

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    index = df.index

    breaks: list[StructureBreak] = []
    trend: str | None = None
    live_high: tuple[float, int] | None = None   # (price, pivot idx)
    live_low: tuple[float, int] | None = None

    for j in range(n):
        up_probe = closes[j] if use_close else highs[j]
        down_probe = closes[j] if use_close else lows[j]

        if live_high is not None and up_probe > live_high[0]:
            # Breaking up out of a downtrend is the character change; breaking
            # up while already up (or with no trend yet) is continuation.
            kind = "choch" if trend == "down" else "bos"
            breaks.append(StructureBreak(
                idx=j, ts=index[j].strftime("%Y-%m-%dT%H:%M:%SZ"),
                kind=kind, direction="bullish",
                level=live_high[0], pivot_idx=live_high[1],
            ))
            trend = "up"
            live_high = None       # consumed; fires once

        elif live_low is not None and down_probe < live_low[0]:
            kind = "choch" if trend == "up" else "bos"
            breaks.append(StructureBreak(
                idx=j, ts=index[j].strftime("%Y-%m-%dT%H:%M:%SZ"),
                kind=kind, direction="bearish",
                level=live_low[0], pivot_idx=live_low[1],
            ))
            trend = "down"
            live_low = None

        # Pivots confirmed *by* this bar become available to the next one.
        for p in by_confirm.get(j, []):
            if p.kind == "high":
                if live_high is None or p.price > live_high[0]:
                    live_high = (p.price, p.idx)
            else:
                if live_low is None or p.price < live_low[0]:
                    live_low = (p.price, p.idx)

    return breaks


def latest_structure_break(
    df: pd.DataFrame,
    *,
    max_age_bars: int | None = None,
    left: int | None = None,
    right: int | None = None,
    use_close: bool = True,
) -> StructureBreak | None:
    """Most recent break, optionally rejected if older than ``max_age_bars``."""
    breaks = detect_structure_breaks(df, left=left, right=right,
                                     use_close=use_close)
    if not breaks:
        return None
    last = breaks[-1]
    if max_age_bars is not None and (len(df) - 1 - last.idx) > max_age_bars:
        return None
    return last


def current_trend(
    df: pd.DataFrame,
    *,
    left: int | None = None,
    right: int | None = None,
) -> str | None:
    """Structural trend implied by the last break: "up", "down", or None."""
    last = latest_structure_break(df, left=left, right=right)
    if last is None:
        return None
    return "up" if last.direction == "bullish" else "down"
