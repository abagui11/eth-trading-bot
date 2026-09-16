"""eva_swing_mech — control's entries, swing geometry. No LLM, no new tokens.

This is the cleanest arm in the experiment and the direct forward test of the
claim that Eva's stops are too tight: it takes **every** entry control takes,
at the same price and the same direction, and changes nothing but the exit
envelope. Any divergence in outcome is therefore attributable to bracket
geometry alone. No prompt change means no confound from the LLM behaving
differently, and no token cost.

The stop is not "control's stop, times a constant". It is placed beyond the H4
swing that would actually invalidate the thesis, plus a volatility buffer, then
clamped. Widening by a fixed multiple would test "does a wider stop help",
which is a weaker and less interesting question than "does a *structural* stop
help" — a fixed multiple is still arbitrary, just arbitrary further away.
"""

from __future__ import annotations

import logging

import bot_config
import eva_variants
import research
from patterns.swing import find_pivots

logger = logging.getLogger(__name__)

def _cfg(name: str, default):
    return getattr(bot_config, name, default)


def _atr(df, period: int = 14) -> float:
    """Wilder-ish ATR on the supplied frame; plain range mean is close enough
    here because it is only ever used as a buffer, not as a level."""
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    if len(df) < 2:
        return 0.0
    trs = []
    for i in range(1, len(df)):
        trs.append(max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        ))
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


def structural_stop(product_id: str, side: str, entry: float) -> tuple[float, str]:
    """Stop beyond the H4 swing that invalidates a trade at ``entry``.

    Falls back to a percentage stop when H4 data or pivots are unavailable —
    a missing candle feed must not silently produce a reckless bracket.
    """
    min_pct = float(_cfg("EVA_SWING_MIN_STOP_PCT", 0.010))   # 1.0%
    max_pct = float(_cfg("EVA_SWING_MAX_STOP_PCT", 0.045))   # 4.5%
    fallback_pct = float(_cfg("EVA_SWING_FALLBACK_STOP_PCT", 0.025))
    sign = 1.0 if side == "long" else -1.0

    def clamp(dist: float, why: str) -> tuple[float, str]:
        d = min(max(dist, entry * min_pct), entry * max_pct)
        return entry - sign * d, why

    try:
        df = research.to_dataframe(research.get_ohlc("H4", product_id=product_id))
    except Exception:
        logger.warning("eva_swing: H4 fetch failed for %s", product_id,
                       exc_info=True)
        return clamp(entry * fallback_pct, "fallback_no_h4")

    pivots = find_pivots(df, left=4, right=4)
    buffer = _atr(df) * float(_cfg("EVA_SWING_ATR_BUFFER", 0.5))

    if side == "long":
        lows = [p.price for p in pivots if p.kind == "low" and p.price < entry]
        if not lows:
            return clamp(entry * fallback_pct, "fallback_no_pivot")
        level = max(lows)          # nearest swing low beneath entry
        return clamp(entry - (level - buffer), "h4_swing_low")

    highs = [p.price for p in pivots if p.kind == "high" and p.price > entry]
    if not highs:
        return clamp(entry * fallback_pct, "fallback_no_pivot")
    level = min(highs)             # nearest swing high above entry
    return clamp((level + buffer) - entry, "h4_swing_high")


def swing_targets(side: str, entry: float, stop: float) -> list[float]:
    """Control's three-rung ladder shape at swing distances.

    Same *shape* as control on purpose: if the rung count changed too, a
    difference in realized R could come from the ladder rather than the reach,
    and the two would be impossible to separate in a book this small.
    """
    rungs = _cfg("EVA_SWING_TP_RUNGS", (1.5, 3.0, 5.0))
    risk = abs(entry - stop)
    sign = 1.0 if side == "long" else -1.0
    return [entry + sign * risk * float(r) for r in rungs]


def mirror(suggestion, *, cycle_id: str | None = None) -> int | None:
    """Open the swing mirror of a control suggestion. Returns position id."""
    if not _cfg("EVA_VARIANTS_ENABLED", False):
        return None
    side = eva_variants.side_of_action(getattr(suggestion, "action", None))
    if side is None:
        return None

    product_id = getattr(suggestion, "product_id", None) or "ETH-USD"
    try:
        entry = float(getattr(suggestion, "entry", 0) or 0)
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None

    # One swing position per product per side. A swing book that stacks four
    # mirrors of the same recurring thesis would report one idea four times.
    if eva_variants.has_open(eva_variants.SWING_MECH, product_id, side):
        eva_variants.record_skip(
            eva_variants.SWING_MECH, reason="cooldown",
            product_id=product_id, side=side, trigger_name="vision",
        )
        return None

    stop, why = structural_stop(product_id, side, entry)
    tps = swing_targets(side, entry, stop)
    return eva_variants.open_position(
        eva_variants.SWING_MECH,
        product_id=product_id,
        side=side,
        entry=entry,
        stop_loss=stop,
        take_profits=tps,
        entry_source="vision_mirror",
        cycle_id=cycle_id,
        trigger_name="vision",
        rationale=f"Mechanical swing re-bracket ({why})",
        max_hold_hours=float(_cfg("EVA_SWING_MAX_HOLD_H", 168.0)),  # 7 days
    )
