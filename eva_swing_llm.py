"""eva_swing_llm — the swing mandate arm. Its own vision call, every 2 hours.

Why a separate call rather than folding H12/D1 into the 30-minute cycle: a
single Claude call shows every image to the model, so there is no way to give
the swing mandate higher-timeframe context while hiding it from control. Adding
the charts to the shared call would change what control sees and control would
stop being a control — which defeats the experiment. A dedicated call keeps
control byte-identical.

The cadence is 2h, not 30min, because this book is explicitly looking for
multi-day structure. Finer resolution would buy it nothing and cost 4x the
tokens: ~12 calls/day instead of 48.

The prompt differs from control in exactly one respect that matters: it is told
to place the stop where the *thesis* is wrong on H4/H12, not at a fixed
percentage, and to aim at the next higher-timeframe liquidity objective.
"""

from __future__ import annotations

import json
import logging
import re

import anthropic

import analyze
import bot_config
import charts
import config
import eva_variants
import research
from patterns.htf_structure import detect_htf_zones
from patterns.key_levels import compute_key_levels

logger = logging.getLogger(__name__)

MAX_TOKENS = 2048

_SYSTEM = """You are Eva, running a **swing** mandate on crypto majors.

This is not the intraday book. You are looking for positions that need days,
not hours, to resolve. Your edge is ICT market structure on the higher
timeframes: order blocks, fair value gaps, liquidity sweeps, and the draw on
liquidity implied by unfilled highs and lows.

Two rules define this mandate, and they are what make it different:

1. **The stop goes where the thesis is wrong, not at a fixed percentage.**
   Place it beyond the H4 or H12 swing / order block whose violation would mean
   you had read the structure incorrectly. If that level is 3% away, the stop is
   3% away. A stop placed closer than the structure warrants will be taken out
   by noise before the idea resolves, which is the specific failure this book
   exists to avoid.

2. **Targets are higher-timeframe liquidity objectives.** Aim at the next H12 or
   D1 swing, unfilled gap, or obvious pool of stops. Three targets, ordered
   nearest to furthest. Intermediate levels may sit closer than 1x your risk —
   that is fine, and expected once the stop is structural — but your **furthest
   target must be at least 2x your risk**, or the reach does not justify a
   swing hold.

Expect to hold for days. A setup that needs to work within four hours is not a
swing trade — return no_trade for it.

Return JSON only, no prose outside it."""

_USER_TEMPLATE = """Analyze {product} for a swing position.

Charts attached, highest timeframe first: {tf_list}.

Current price: {price:.2f}

Return JSON only:
{{"action": "long" | "short" | "no_trade",
  "entry": 0,
  "stop_loss": 0,
  "take_profits": [0, 0, 0],
  "invalidation": "the H4/H12 level whose violation kills this thesis, and why",
  "objective": "the HTF liquidity the trade is drawing toward",
  "rationale": "under 400 characters"}}"""


def _cfg(name: str, default):
    return getattr(bot_config, name, default)


def _extract_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def validate_swing_plan(
    side: str, entry: float, stop: float, tps: list[float]
) -> tuple[bool, str]:
    """Reject plans the mandate should never have produced.

    The point of this arm is a *structural* stop, which means the clamps cannot
    be tight — but an unclamped LLM can still mint a stop 15% away (which makes
    the position meaningless after equal-risk sizing) or 0.2% away (which is
    just control with extra steps). Both get rejected rather than silently
    coerced: a coerced plan is no longer the plan the model proposed, and
    scoring it would misattribute the result.
    """
    min_pct = float(_cfg("EVA_SWING_MIN_STOP_PCT", 0.010))
    max_pct = float(_cfg("EVA_SWING_MAX_STOP_PCT", 0.045))

    if entry <= 0 or stop <= 0 or not tps:
        return False, "missing_levels"
    if side == "long" and stop >= entry:
        return False, "stop_wrong_side"
    if side == "short" and stop <= entry:
        return False, "stop_wrong_side"

    stop_pct = abs(entry - stop) / entry
    if stop_pct < min_pct:
        return False, f"stop_too_tight:{stop_pct:.4f}"
    if stop_pct > max_pct:
        return False, f"stop_too_wide:{stop_pct:.4f}"

    risk = abs(entry - stop)
    ordered = sorted(tps, reverse=(side == "short"))
    for tp in ordered:
        if side == "long" and tp <= entry:
            return False, "target_behind_entry"
        if side == "short" and tp >= entry:
            return False, "target_behind_entry"

    # The reach test is on the *furthest* rung, not the nearest. Once the stop
    # is structural (and so wide), a genuine intermediate objective can sit
    # under 1R — the first real plan this arm produced put TP1 at 0.84R on a
    # 3.2% stop, aimed at Monday High, and was correct to. Demanding a distant
    # TP1 would reject coherent swing geometry and starve the book of data,
    # which is the failure mode that ends an experiment early.
    min_last_r = float(_cfg("EVA_SWING_MIN_LAST_TARGET_R", 2.0))
    min_first_r = float(_cfg("EVA_SWING_MIN_FIRST_TARGET_R", 0.5))
    if abs(ordered[0] - entry) / risk < min_first_r:
        return False, f"first_target_under_{min_first_r}R"
    if abs(ordered[-1] - entry) / risk < min_last_r:
        return False, f"last_target_under_{min_last_r}R"
    return True, "ok"


def _charts_for(product_id: str) -> tuple[list[str], float]:
    """Render the swing timeframes. Returns (paths, latest price)."""
    timeframes = list(_cfg("EVA_SWING_LLM_TIMEFRAMES", ("D1", "H12", "H4")))
    paths: list[str] = []
    price = 0.0
    daily = research.get_daily_bars_for_levels(product_id=product_id)
    levels = compute_key_levels(daily)
    h4 = research.get_ohlc("H4", product_id=product_id)
    zones = detect_htf_zones(h4, product_id=product_id)
    for tf in timeframes:
        bars = h4 if tf == "H4" else research.get_ohlc(tf, product_id=product_id)
        if not bars:
            continue
        price = float(bars[-1]["close"])
        paths.append(charts.render_stance_marked_chart(
            bars, product_id=product_id, timeframe=tf,
            key_levels=levels, htf_zones=zones, stance="swing",
        ))
    return paths, price


def propose(product_id: str) -> dict | None:
    """One swing vision call for one product. Returns the parsed plan or None."""
    paths, price = _charts_for(product_id)
    if not paths or price <= 0:
        logger.warning("eva_swing_llm: no charts for %s", product_id)
        return None

    timeframes = list(_cfg("EVA_SWING_LLM_TIMEFRAMES", ("D1", "H12", "H4")))
    content: list[dict] = [{
        "type": "text",
        "text": _USER_TEMPLATE.format(
            product=product_id, tf_list=", ".join(timeframes), price=price,
        ),
    }]
    for tf, path in zip(timeframes, paths):
        content.append({"type": "text", "text": f"{product_id} {tf}:"})
        content.append(analyze._image_block(path))

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": _SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
        )
    except Exception:
        logger.exception("eva_swing_llm: API call failed for %s", product_id)
        return None

    try:
        analyze.log_anthropic_usage(response, "eva_swing_llm")
    except Exception:
        logger.debug("eva_swing_llm: usage logging failed", exc_info=True)

    text = "".join(
        block.text for block in response.content
        if getattr(block, "type", "") == "text"
    )
    plan = _extract_json(text)
    if plan is None:
        logger.warning("eva_swing_llm: unparseable response for %s", product_id)
        return None
    plan["_price"] = price
    plan["_product_id"] = product_id
    return plan


def run_swing_llm_cycle() -> dict:
    """Scheduler entry point for the swing mandate arm."""
    if not (_cfg("EVA_VARIANTS_ENABLED", False)
            and _cfg("EVA_SWING_LLM_ENABLED", False)):
        return {"enabled": False}

    opened: list[int] = []
    for product_id in bot_config.TRADED_PRODUCTS:
        try:
            plan = propose(product_id)
        except Exception:
            logger.exception("eva_swing_llm: propose failed for %s", product_id)
            continue
        if not plan:
            continue

        action = str(plan.get("action") or "").lower()
        if action not in ("long", "short"):
            eva_variants.record_skip(
                eva_variants.SWING_LLM, reason="no_trade",
                product_id=product_id, trigger_name="swing_vision",
            )
            continue

        try:
            entry = float(plan.get("entry") or 0)
            stop = float(plan.get("stop_loss") or 0)
            tps = [float(t) for t in (plan.get("take_profits") or [])]
        except (TypeError, ValueError):
            eva_variants.record_skip(
                eva_variants.SWING_LLM, reason="unparseable_levels",
                product_id=product_id, side=action,
            )
            continue

        ok, why = validate_swing_plan(action, entry, stop, tps)
        if not ok:
            eva_variants.record_skip(
                eva_variants.SWING_LLM, reason=f"rejected:{why}",
                product_id=product_id, side=action, trigger_name="swing_vision",
            )
            logger.info("eva_swing_llm: rejected %s plan — %s", product_id, why)
            continue

        if eva_variants.has_open(eva_variants.SWING_LLM, product_id, action):
            eva_variants.record_skip(
                eva_variants.SWING_LLM, reason="cooldown",
                product_id=product_id, side=action, trigger_name="swing_vision",
            )
            continue

        pid = eva_variants.open_position(
            eva_variants.SWING_LLM,
            product_id=product_id,
            side=action,
            entry=entry,
            stop_loss=stop,
            take_profits=tps,
            entry_source="swing_vision",
            trigger_name="swing_vision",
            rationale=str(plan.get("rationale") or "")[:400],
            max_hold_hours=float(_cfg("EVA_SWING_MAX_HOLD_H", 168.0)),
        )
        if pid:
            opened.append(pid)

    return {"enabled": True, "opened": opened}
