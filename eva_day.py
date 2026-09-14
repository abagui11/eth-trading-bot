"""eva_day — the fast ICT variant. Paper only, and costs nothing in tokens.

The bot never forms a directional opinion. Eva's 30-minute vision call already
produced one; this module only *times* it, on M1, between cycles. That is what
satisfies the "no new vision calls" constraint: every trigger here is
arithmetic on candles already free to fetch.

Two entry sources, tagged and scored separately because only one of them is
supported by the recorded book:

``vision_rebracket``
    A mirror of a real vision suggestion with the day bracket applied (tight
    structural stop, single near target, 4h cap). This is the path the §1.3
    evidence in EVA_VARIANTS_PLAN.md actually supports.

``m1_trigger``
    Deterministic: an M1 structure break in the stance direction, followed by a
    retrace into an unmitigated FVG. This is the *hypothesis*, not a validated
    edge, and it is expected to be the noisier of the two.

Keeping them apart matters. Pooled, a good mirror result would launder a bad
trigger result into something that looks like a validated strategy.
"""

from __future__ import annotations

import logging

import bot_config
import eva_variants
import ledger
import research
from patterns.fvg import detect_fvgs
from patterns.structure_shift import detect_structure_breaks

logger = logging.getLogger(__name__)

# A break older than this is stale — the retrace it was supposed to cause has
# either happened or the move is over.
MAX_BREAK_AGE_BARS = 30      # M1 bars
PIVOT_L = PIVOT_R = 3        # tighter than the H4 default; M1 swings are small

def _cfg(name: str, default):
    return getattr(bot_config, name, default)


def stance_for(product_id: str) -> tuple[str | None, str]:
    """Eva's current directional read for a product, plus why.

    Returns ``(side | None, reason)``. ``None`` means the day bot must stand
    down: no stance, or the last read was an explicit no_trade.
    """
    row = ledger.get_latest_for_product(product_id)
    if row is None:
        return None, "no_stance"
    action = str(row.get("action") or "").lower()
    if action == "no_trade":
        return None, "stance_no_trade"
    side = eva_variants.side_of_action(action)
    if side is None:
        return None, f"stance_unmapped:{action}"
    return side, "ok"


def _day_bracket(side: str, entry: float, stop: float) -> tuple[float, list[float]]:
    """Clamp the stop to sane bounds and put a single target at R multiple.

    The day book intentionally runs one near target rather than a ladder: at a
    4h horizon there is not room for three rungs, and a ladder would make its
    R indistinguishable from control's for the wrong reason.
    """
    min_pct = float(_cfg("EVA_DAY_MIN_STOP_PCT", 0.0015))   # 0.15%
    max_pct = float(_cfg("EVA_DAY_MAX_STOP_PCT", 0.0060))   # 0.60%
    tp_r = float(_cfg("EVA_DAY_TP_R", 1.0))

    dist = abs(entry - stop)
    dist = max(dist, entry * min_pct)
    dist = min(dist, entry * max_pct)
    sign = 1.0 if side == "long" else -1.0
    stop_px = entry - sign * dist
    target = entry + sign * dist * tp_r
    return stop_px, [target]


def _try_open(
    *,
    product_id: str,
    side: str,
    entry: float,
    raw_stop: float,
    source: str,
    trigger_name: str,
    rationale: str,
    cycle_id: str | None = None,
) -> int | None:
    """Cooldown check, bracket, open. Every rejection is recorded as a skip."""
    if eva_variants.has_open(eva_variants.DAY, product_id, side):
        eva_variants.record_skip(
            eva_variants.DAY, reason="cooldown", product_id=product_id,
            side=side, trigger_name=trigger_name,
        )
        return None
    stop_px, tps = _day_bracket(side, entry, raw_stop)
    return eva_variants.open_position(
        eva_variants.DAY,
        product_id=product_id,
        side=side,
        entry=entry,
        stop_loss=stop_px,
        take_profits=tps,
        entry_source=source,
        cycle_id=cycle_id,
        trigger_name=trigger_name,
        rationale=rationale,
        max_hold_hours=float(_cfg("EVA_DAY_MAX_HOLD_H", 4.0)),
    )


def scan_m1_triggers(product_id: str) -> int | None:
    """One product's deterministic M1 trigger. Returns a position id or None.

    The conjunction is deliberate. Measured on live M1, structure breaks alone
    fire ~1.3x/hour per product and FVGs ~4x/hour — either on its own would
    open dozens of positions a day. Requiring a break *in the stance direction*
    followed by a retrace into an unmitigated gap is what makes the rate
    tradeable.
    """
    side, why = stance_for(product_id)
    if side is None:
        eva_variants.record_skip(eva_variants.DAY, reason=why,
                                 product_id=product_id)
        return None

    try:
        df = research.to_dataframe(
            research.get_ohlc("M1", product_id=product_id)
        )
    except Exception:
        logger.warning("eva_day: M1 fetch failed for %s", product_id,
                       exc_info=True)
        return None
    if len(df) < 60:
        return None

    want = "bullish" if side == "long" else "bearish"
    last_idx = len(df) - 1

    breaks = [
        b for b in detect_structure_breaks(df, left=PIVOT_L, right=PIVOT_R)
        if b.direction == want and (last_idx - b.idx) <= MAX_BREAK_AGE_BARS
    ]
    if not breaks:
        eva_variants.record_skip(
            eva_variants.DAY, reason="no_structure_break",
            product_id=product_id, side=side,
        )
        return None
    brk = breaks[-1]

    # The gap must have formed with the displacement, i.e. at or after the
    # break — an older gap is unrelated context, not this move's imbalance.
    gaps = [
        g for g in detect_fvgs(df, open_only=True)
        if g.direction == want and g.idx >= brk.idx
    ]
    if not gaps:
        eva_variants.record_skip(
            eva_variants.DAY, reason="no_open_fvg",
            product_id=product_id, side=side, trigger_name=f"{brk.kind}+fvg",
        )
        return None
    gap = min(gaps, key=lambda g: abs(g.midpoint - float(df["close"].iloc[-1])))

    price = float(df["close"].iloc[-1])
    # Entry is consequent encroachment (the gap's 50%), the level ICT treats as
    # the fill. If price has not reached it yet there is nothing to do; the
    # next scan will look again while the gap stays open.
    if not gap.contains(price):
        eva_variants.record_skip(
            eva_variants.DAY, reason="price_outside_fvg",
            product_id=product_id, side=side, trigger_name=f"{brk.kind}+fvg",
        )
        return None

    # Stop sits beyond the gap — through it, the imbalance is spent and the
    # reason for the trade is gone.
    raw_stop = gap.bottom if side == "long" else gap.top
    return _try_open(
        product_id=product_id,
        side=side,
        entry=price,
        raw_stop=raw_stop,
        source="m1_trigger",
        trigger_name=f"{brk.kind}_{want}+fvg",
        rationale=(
            f"M1 {brk.kind} {want} through {brk.level:.2f} "
            f"(bar -{last_idx - brk.idx}), retrace into open FVG "
            f"{gap.bottom:.2f}–{gap.top:.2f}; stance {side}"
        ),
    )


def mirror_vision_suggestion(suggestion, *, cycle_id: str | None = None) -> int | None:
    """Re-bracket a live vision suggestion into the day book.

    Same entry as control, day geometry. Called from the cycle, so it inherits
    whatever the LLM decided about direction and entry and changes only the
    exit envelope.
    """
    side = eva_variants.side_of_action(getattr(suggestion, "action", None))
    if side is None:
        return None
    product_id = getattr(suggestion, "product_id", None) or "ETH-USD"
    try:
        entry = float(getattr(suggestion, "entry", 0) or 0)
        stop = float(getattr(suggestion, "stop_loss", 0) or 0)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or stop <= 0:
        return None

    return _try_open(
        product_id=product_id,
        side=side,
        entry=entry,
        raw_stop=stop,
        source="vision_rebracket",
        trigger_name="vision",
        rationale="Day re-bracket of vision suggestion",
        cycle_id=cycle_id,
    )


def run_day_scan() -> dict:
    """Scheduler entry point: resolve open day positions, then look for entries."""
    if not _cfg("EVA_VARIANTS_ENABLED", False):
        return {"enabled": False}

    closed = 0
    try:
        closed = eva_variants.mark_to_market()
    except Exception:
        logger.exception("eva_day: mark_to_market failed")

    opened: list[int] = []
    if _cfg("EVA_DAY_M1_TRIGGERS_ENABLED", True):
        for product_id in bot_config.TRADED_PRODUCTS:
            try:
                pid = scan_m1_triggers(product_id)
                if pid:
                    opened.append(pid)
            except Exception:
                logger.exception("eva_day: scan failed for %s", product_id)

    return {"enabled": True, "closed": closed, "opened": opened}
