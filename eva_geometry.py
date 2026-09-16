"""Volatility-conditioned SL/TP for Eva HQ suggestions — all active books.

Why this exists (measured 2026-09-16, analysis/_q0916_dynamic_geometry.py in
trade_ideas): across the 52-entry recorded book, Eva's stop width was
uncalibrated to the tape — stop/ATR24 IQR 3.0x–7.0x for the same strategy —
while the room a right-thesis trade actually needed before paying scaled with
ATR24 (median 5.1x, p75 9.9x). Her median stop (4.9x) sat *under* the median
room needed, so roughly half the correct reads were stopped by ordinary noise
(the September S1/S2/S4 losers are exactly this). Meanwhile recorded TP1s sat
at a median 9.1x ATR24 against a 48h favorable-excursion p25 of 4.5x — targets
priced for a louder tape than the one the trade lives in.

The rule, replayed on the same engine as every other Eva measurement
(M5 re-walk, stop-first ties, 48h horizon, constant $ risk):

    stop distance  = max(LLM stop, STOP_FLOOR_ATR x ATR24)   — never tightened
    TP rung k dist = min(LLM dist, k x TP_CAP_ATR x ATR24)   — never widened

At (floor 7, cap 8): mean R +0.128 -> +0.251, placebo delta +0.33, high-vol
half +0.21 -> +0.50, S1/S2 flip to winners and S4 survives unstopped. Honest
cost: 11 tail winners truncated (a +6.3R July runner becomes +1.75R) — none
flip negative. n=52, so this is calibration to the tape's measured noise
envelope, not proof of P&L; the floor sits at the book's own p75 stop ratio
and the cap between p25 and p50 of favorable excursion — structural anchors,
not swept optima. The (floor, cap) plateau is broad (5–7 x 6–8 all beat
baseline), which is what makes it shippable.

ATR24 here is the study's definition exactly: trailing-24h mean M5 bar range
as a fraction of price. If candles cannot be fetched the suggestion's own
levels stand — this module only ever refuses to act, never guesses.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import bot_config
import research

logger = logging.getLogger(__name__)

_MIN_BARS = 200  # ~17h of M5; below this the ATR estimate is not trustworthy


def _cfg(name: str, default):
    return getattr(bot_config, name, default)


def atr24_pct(product_id: str) -> float | None:
    """Trailing-24h mean M5 true range as % of price; None when unavailable."""
    end = int(time.time())
    start = end - 86400 - 900
    try:
        bars = research.fetch_coinbase_candles_range(
            "FIVE_MINUTE", start, end, product_id=product_id
        )
    except Exception:
        logger.exception("eva_geometry: M5 fetch failed for %s", product_id)
        return None
    if len(bars) < _MIN_BARS:
        logger.warning(
            "eva_geometry: only %d M5 bars for %s — leaving levels untouched",
            len(bars), product_id,
        )
        return None
    total = 0.0
    n = 0
    for b in bars:
        close = float(b["close"])
        if close <= 0:
            continue
        total += (float(b["high"]) - float(b["low"])) / close
        n += 1
    if n < _MIN_BARS:
        return None
    return total / n * 100.0


def condition_levels(
    side: str,
    entry: float,
    stop_loss: float,
    take_profits: list[float],
    atr_pct: float | None,
) -> tuple[float, list[float], dict[str, Any]]:
    """Apply the floor/cap rule; pure function so it is directly testable.

    Returns (stop, take_profits, info). When ``atr_pct`` is None the inputs
    come back unchanged with info["applied"] = False.
    """
    info: dict[str, Any] = {
        "applied": False, "atr24_pct": atr_pct,
        "stop_widened": False, "tps_capped": 0,
        "orig_stop": stop_loss, "orig_tps": list(take_profits),
    }
    if atr_pct is None or atr_pct <= 0 or entry <= 0 or not take_profits:
        return stop_loss, list(take_profits), info

    sign = 1.0 if side == "long" else -1.0
    atr_frac = atr_pct / 100.0
    floor_mult = float(_cfg("EVA_GEOM_STOP_FLOOR_ATR", 7.0))
    cap_mult = float(_cfg("EVA_GEOM_TP_CAP_ATR", 8.0))
    max_stop = float(_cfg("EVA_GEOM_MAX_STOP_PCT", 0.045))

    # --- stop: floored at floor_mult x ATR24, never tightened, hard ceiling.
    rec_dist = abs(entry - stop_loss)
    floor_dist = min(floor_mult * atr_frac, max_stop) * entry
    new_dist = max(rec_dist, floor_dist)
    new_stop = entry - sign * new_dist
    if new_dist > rec_dist * 1.0001:
        info["stop_widened"] = True

    # --- targets: rung k capped at k x cap_mult x ATR24, never widened.
    # Work in distance space sorted nearest-first, keep the ladder monotone.
    dists = sorted(abs(tp - entry) for tp in take_profits)
    capped: list[float] = []
    running = 0.0
    for k, d in enumerate(dists, start=1):
        cap = k * cap_mult * atr_frac * entry
        nd = min(d, cap)
        if nd < d * 0.9999:
            info["tps_capped"] += 1
        running = max(running, nd)  # a capped rung never re-orders the ladder
        capped.append(running)
    new_tps = [entry + sign * d for d in capped]

    info["applied"] = True
    return new_stop, new_tps, info


def apply_to_suggestion(suggestion) -> dict[str, Any] | None:
    """Mutate a finalized HQ suggestion in place. Returns info or None."""
    if suggestion.action == "no_trade":
        return None
    if not (suggestion.entry and suggestion.stop_loss and suggestion.take_profits):
        return None
    side = "long" if "buy" in suggestion.action else "short"
    atr = atr24_pct(suggestion.product_id)
    stop, tps, info = condition_levels(
        side, float(suggestion.entry), float(suggestion.stop_loss),
        [float(t) for t in suggestion.take_profits], atr,
    )
    if not info["applied"]:
        return info
    if info["stop_widened"] or info["tps_capped"]:
        logger.info(
            "eva_geometry %s %s: ATR24 %.3f%% | stop %.2f -> %.2f (%s) | "
            "tps %s -> %s (%d capped)",
            suggestion.product_id, side, atr,
            info["orig_stop"], stop,
            "floored" if info["stop_widened"] else "kept",
            [round(t, 2) for t in info["orig_tps"]],
            [round(t, 2) for t in tps], info["tps_capped"],
        )
    suggestion.stop_loss = stop
    suggestion.take_profits = tps
    return info
