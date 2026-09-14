"""Fair Value Gap detection — causal (live-safe) three-candle definition.

An FVG is the unfilled window left when price displaces so fast that the
middle candle's body skips a range no trade occurred in. ICT treats the
unfilled remainder as a magnet: price tends to return and "rebalance" it.

**Why this is not `smartmoneyconcepts.fvg()`.** That implementation reads
``ohlc["low"].shift(-1)`` — it flags the gap on the *middle* candle using the
*next* candle's data, so at the close of the bar it marks, the signal is not
yet knowable. Consuming it live is lookahead bias. Here the gap is attributed
to the third candle, the bar that completes it, and every field is computable
at that bar's close.

Gap geometry (bullish, mirrored for bearish):

    bar i-2  ─┬─ high                  <- gap bottom
              │   (no trade here)
    bar i     ─┴─ low                  <- gap top
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Below this the "gap" is tick noise, especially on M1 where spread alone
# clears it. Expressed as a fraction of price.
MIN_GAP_PCT = 0.0004  # 0.04%


@dataclass
class FVG:
    idx: int          # index of the third candle — the bar that completes it
    ts: str
    direction: str    # "bullish" | "bearish"
    top: float
    bottom: float
    mitigated_idx: int | None = None   # first later bar to trade into it

    @property
    def size_pct(self) -> float:
        mid = (self.top + self.bottom) / 2
        return (self.top - self.bottom) / mid if mid else 0.0

    @property
    def midpoint(self) -> float:
        """Consequent encroachment — the 50% level ICT uses as the entry."""
        return (self.top + self.bottom) / 2

    @property
    def is_open(self) -> bool:
        return self.mitigated_idx is None

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


def detect_fvgs(
    df: pd.DataFrame,
    *,
    min_gap_pct: float = MIN_GAP_PCT,
    open_only: bool = False,
) -> list[FVG]:
    """Find three-candle FVGs, oldest first, each marked with its mitigation.

    ``open_only`` returns just the gaps price has not yet traded back into,
    which are the only ones that can still act as an entry zone.
    """
    n = len(df)
    if n < 3:
        return []

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    index = df.index

    found: list[FVG] = []
    for i in range(2, n):
        bullish = lows[i] > highs[i - 2] and closes[i] > opens[i]
        bearish = highs[i] < lows[i - 2] and closes[i] < opens[i]
        if not (bullish or bearish):
            continue

        if bullish:
            bottom, top, direction = highs[i - 2], lows[i], "bullish"
        else:
            bottom, top, direction = highs[i], lows[i - 2], "bearish"

        gap = FVG(
            idx=i,
            ts=index[i].strftime("%Y-%m-%dT%H:%M:%SZ"),
            direction=direction,
            top=float(top),
            bottom=float(bottom),
        )
        if gap.size_pct < min_gap_pct:
            continue

        # Mitigation: the first subsequent bar whose range enters the window.
        # Entering at all counts — a partial fill already spends some of the
        # imbalance, so treating it as pristine would overstate the edge.
        for j in range(i + 1, n):
            if lows[j] <= gap.top and highs[j] >= gap.bottom:
                gap.mitigated_idx = j
                break
        found.append(gap)

    if open_only:
        return [g for g in found if g.is_open]
    return found


def nearest_open_fvg(
    df: pd.DataFrame,
    price: float,
    direction: str | None = None,
    *,
    min_gap_pct: float = MIN_GAP_PCT,
) -> FVG | None:
    """The unmitigated gap closest to ``price``, optionally filtered by side."""
    gaps = detect_fvgs(df, min_gap_pct=min_gap_pct, open_only=True)
    if direction:
        gaps = [g for g in gaps if g.direction == direction]
    if not gaps:
        return None
    return min(gaps, key=lambda g: abs(g.midpoint - price))
