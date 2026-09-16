"""eva_geom — control's entries, vol-conditioned brackets. Paper only.

The fourth writable book. Same pattern as eva_swing_mech: it takes the exact
suggestion control just booked and replaces only the exit envelope, so the
comparison isolates geometry — any R difference against control is bracket
placement and nothing else.

The rule (eva_geometry.condition_levels, measured 2026-09-16 on the 52-entry
recorded book): stop floored at EVA_GEOM_STOP_FLOOR_ATR x trailing-24h ATR
(never tightened), TP rung k capped at k x EVA_GEOM_TP_CAP_ATR x ATR (never
widened). It replay-won (+0.128 -> +0.251 mean R, placebo delta +0.33) but it
overrides the LLM's structural ICT levels, which is an untested hypothesis —
hence a paper book, not a control change.

When ATR is unavailable the mirror SKIPS rather than opening control's raw
geometry under this book's name: an unconditioned twin would dilute the very
comparison the book exists to make.
"""
from __future__ import annotations

import logging

import bot_config
import eva_geometry
import eva_variants

logger = logging.getLogger(__name__)


def _cfg(name: str, default):
    return getattr(bot_config, name, default)


def mirror(suggestion, *, cycle_id: str | None = None) -> int | None:
    """Open the vol-conditioned twin of a control suggestion. None = no open."""
    if not _cfg("EVA_GEOM_ENABLED", False):
        return None
    side = eva_variants.side_of_action(getattr(suggestion, "action", None))
    if side is None:
        return None
    entry = float(suggestion.entry or 0)
    stop = float(suggestion.stop_loss or 0)
    tps = [float(t) for t in (suggestion.take_profits or [])]
    if entry <= 0 or stop <= 0 or not tps:
        return None
    product_id = suggestion.product_id

    if eva_variants.has_open(eva_variants.GEOM, product_id, side):
        eva_variants.record_skip(
            eva_variants.GEOM, reason="cooldown",
            product_id=product_id, side=side, trigger_name="geom_mirror",
        )
        return None

    atr = eva_geometry.atr24_pct(product_id)
    if atr is None:
        # No ATR, no position: booking control's raw brackets under this book
        # would blur the one comparison it exists to make.
        eva_variants.record_skip(
            eva_variants.GEOM, reason="no_atr",
            product_id=product_id, side=side, trigger_name="geom_mirror",
        )
        return None

    new_stop, new_tps, info = eva_geometry.condition_levels(
        side, entry, stop, tps, atr
    )
    return eva_variants.open_position(
        eva_variants.GEOM,
        product_id=product_id,
        side=side,
        entry=entry,
        stop_loss=new_stop,
        take_profits=new_tps,
        entry_source="vision_mirror",
        cycle_id=cycle_id,
        trigger_name="geom_mirror",
        rationale=(
            f"ATR24 {atr:.3f}% | stop "
            f"{'floored' if info['stop_widened'] else 'kept'} "
            f"{stop:.2f}->{new_stop:.2f} | {info['tps_capped']} tp(s) capped"
        ),
        max_hold_hours=float(_cfg("EVA_GEOM_MAX_HOLD_H", 48.0)),
    )
