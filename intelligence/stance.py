"""Hourly multi-horizon stance engine: BTC-first bullish/neutral/bearish views.

Every hour (wall-clock) we compute deterministic features on H4/H1/M15 for
BTC-USD then ETH-USD, ask Claude (fast model) to synthesize stances with the
macro + funding context, and persist the batch. If the LLM call fails the
deterministic stances are persisted instead so the artifact always exists.

The deterministic score is pasted into the model's own prompt as
``programmatic_stance``, so every published row has a counterfactual: what
would have been published had the model kept it. Phase 0 of
``deploy/INTEL_BOARD_PLAN.md`` records that counterfactual on every row
(``STANCE_LOG_COUNTERFACTUAL``) without changing what is published; the two
Phase 1 flags then decide whether a departure is allowed to stand. See
``trade_ideas/analysis/EVA proof of concept/VISION_ACCURACY.md`` for why.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import anthropic

import bot_config
import charts
import config
import research
from intelligence import funding, store
from macro.context import build_macro_block
from patterns.htf_structure import HTFZone, detect_htf_zones
from patterns.key_levels import KeyLevel, compute_key_levels

logger = logging.getLogger(__name__)

# BTC first — its posture leads; ETH is annotated as the follow-on conduit.
STANCE_PRODUCTS: tuple[str, ...] = ("BTC-USD", "ETH-USD")
STANCE_TIMEFRAMES: tuple[str, ...] = ("H4", "H1", "M15")
_MAX_STANCE_TOKENS = 1200


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def compute_timeframe_features(
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministic trend/volume features for one timeframe."""
    closes = [float(b["close"]) for b in bars]
    volumes = [float(b["volume"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    last = closes[-1]

    ema_fast = _ema(closes[-60:], 12)
    ema_slow = _ema(closes[-60:], 26)

    window_high = max(highs[-40:])
    window_low = min(lows[-40:])
    span = window_high - window_low
    range_pos = (last - window_low) / span if span > 0 else 0.5

    # Structure: compare last-10-bar extremes to the prior 10.
    recent_high, prior_high = max(highs[-10:]), max(highs[-20:-10])
    recent_low, prior_low = min(lows[-10:]), min(lows[-20:-10])
    higher_highs = recent_high > prior_high
    higher_lows = recent_low > prior_low
    lower_highs = recent_high < prior_high
    lower_lows = recent_low < prior_low

    vol_mean = sum(volumes[-40:-1]) / max(len(volumes[-40:-1]), 1)
    vol_last_ratio = volumes[-1] / vol_mean if vol_mean > 0 else 1.0

    score = 0
    if ema_fast > ema_slow:
        score += 1
    elif ema_fast < ema_slow:
        score -= 1
    if higher_highs and higher_lows:
        score += 1
    elif lower_highs and lower_lows:
        score -= 1
    if range_pos > 0.7:
        score += 1
    elif range_pos < 0.3:
        score -= 1

    if score >= 2:
        stance = "bullish"
    elif score <= -2:
        stance = "bearish"
    else:
        stance = "neutral"

    return {
        "stance": stance,
        "score": score,
        "last_close": last,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "range_pos": round(range_pos, 3),
        "higher_highs": higher_highs,
        "higher_lows": higher_lows,
        "lower_highs": lower_highs,
        "lower_lows": lower_lows,
        "volume_last_ratio": round(vol_last_ratio, 2),
    }


def gather_bars() -> dict[str, dict[str, list[dict[str, Any]]]]:
    """{product_id: {timeframe: bars}} for the stance products."""
    return {
        product_id: {
            tf: research.get_ohlc(tf, product_id=product_id)
            for tf in STANCE_TIMEFRAMES
        }
        for product_id in STANCE_PRODUCTS
    }


def gather_features(
    bars_by_product: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """{product_id: {timeframe: features}} for the stance products."""
    bars_by_product = bars_by_product or gather_bars()
    return {
        product_id: {
            tf: compute_timeframe_features(bars_by_product[product_id][tf])
            for tf in STANCE_TIMEFRAMES
        }
        for product_id in STANCE_PRODUCTS
    }


def _board_overlays(
    product_id: str,
    bars: dict[str, list[dict[str, Any]]],
) -> tuple[list[KeyLevel], list[HTFZone]]:
    """Calendar key levels + H4 order blocks for one product's board row.

    Either overlay degrades to empty rather than losing the whole chart, so a
    failed daily-candle fetch still leaves candles and swing lines on screen.
    """
    try:
        daily_bars = research.get_daily_bars_for_levels(product_id=product_id)
        key_levels = compute_key_levels(daily_bars)
    except Exception:
        logger.exception("Key levels failed for %s", product_id)
        key_levels = []
    try:
        htf_zones = detect_htf_zones(bars.get("H4") or [], product_id=product_id)
    except Exception:
        logger.exception("H4 zones failed for %s", product_id)
        htf_zones = []
    return key_levels, htf_zones


def render_structure_board(
    bars_by_product: dict[str, dict[str, list[dict[str, Any]]]],
    stances: list[dict[str, Any]],
) -> dict[str, str]:
    """Marked H4/H1/M15 PNGs for the hub. Never fails the stance cycle."""
    by_key = {
        (str(s.get("product_id")), str(s.get("timeframe"))): s for s in stances
    }
    paths: dict[str, str] = {}
    for product_id in STANCE_PRODUCTS:
        bars = bars_by_product.get(product_id) or {}
        key_levels, htf_zones = _board_overlays(product_id, bars)
        for tf in STANCE_TIMEFRAMES:
            stance_row = by_key.get((product_id, tf)) or {}
            try:
                paths[f"{product_id}:{tf}"] = charts.render_stance_marked_chart(
                    bars[tf],
                    product_id=product_id,
                    timeframe=tf,
                    key_levels=key_levels,
                    htf_zones=htf_zones,
                    stance=stance_row.get("stance"),
                    confidence=stance_row.get("confidence"),
                )
            except Exception:
                logger.exception("Stance chart failed for %s %s", product_id, tf)
    return paths


def _funding_context_block() -> str:
    """Current funding regimes for BTC/ETH.

    Never returns an empty string on failure: an unavailable feed is stated in
    the prompt and logged, so a missing model input cannot pass unnoticed.
    """
    lines: list[str] = []
    missing: list[str] = []
    for product_id in STANCE_PRODUCTS:
        status = funding.funding_status(product_id)
        label = bot_config.product_label(product_id)
        if not status["available"]:
            reason = "stale" if status["as_of_ts"] else "no data"
            missing.append(f"{label} ({reason})")
            continue
        lines.append(
            f"{label}: regime={status['regime']} "
            f"streak={status['streak_periods']} periods "
            f"(as of {status['as_of_ts']}, source {status['source'] or 'unknown'})"
        )

    if missing:
        logger.error(
            "Funding context unavailable for %s — stance prompt runs without "
            "that signal",
            ", ".join(missing),
        )

    if not lines:
        return (
            "=== Perp funding regimes (medium-term signal) ===\n"
            "UNAVAILABLE — the funding feed is down for "
            f"{', '.join(missing)}. Do not infer a funding bias; treat this "
            "input as missing rather than neutral."
        )

    block = (
        "=== Perp funding regimes (medium-term signal) ===\n"
        + "\n".join(lines)
        + "\nRules: persistent positive funding = bullish medium-term bias; "
        "persistent negative = bearish; chop = noise (ignore); a first switch "
        "after persistence is a position-switch cue."
    )
    if missing:
        block += f"\nNOTE: no usable funding data for {', '.join(missing)}."
    return block


def _features_block(features: dict[str, dict[str, dict[str, Any]]]) -> str:
    lines: list[str] = []
    for product_id in STANCE_PRODUCTS:
        lines.append(f"=== {product_id} deterministic features ===")
        for tf in STANCE_TIMEFRAMES:
            f = features[product_id][tf]
            lines.append(
                f"[{tf}] close={f['last_close']:,.2f} ema12{'>' if f['ema_fast'] > f['ema_slow'] else '<='}ema26 "
                f"range_pos={f['range_pos']} HH={f['higher_highs']} HL={f['higher_lows']} "
                f"LH={f['lower_highs']} LL={f['lower_lows']} vol_ratio={f['volume_last_ratio']} "
                f"programmatic_stance={f['stance']}"
            )
    return "\n".join(lines)


_STANCE_PROMPT = """You are the Eva intelligence desk. Produce short-horizon
market stances for BTC-USD and ETH-USD on H4, H1, and M15.

Rules:
- BTC first: decide BTC posture before ETH. BTC leads; ETH is a higher-beta
  conduit. If ETH diverges from BTC, the ETH rationale must say why.
- ICT/structure logic applies: respect order-block/structure context implied by
  the deterministic features. Programmatic stances are advisory, not binding.
- Use the macro and funding context if present.
- Each stance is exactly one of: bullish, neutral, bearish.
- confidence is 0.0-1.0.
- rationale: 1-3 short bullet-style sentences.

Return JSON only, with no commentary before or after the object:
{"stances":[{"product_id":"BTC-USD","timeframe":"H4","stance":"bullish",
"confidence":0.7,"rationale":"..."}, ...6 entries total: BTC-USD and ETH-USD
on each of H4, H1, M15...],
"medium_summary":"2-4 sentences on the medium-term picture",
"btc_eth_note":"1-2 sentences on how BTC posture maps to ETH"}
"""

# Appended only when a Phase 1 flag is on, so Phase 0 leaves the prompt — and
# therefore the model's behaviour — byte-identical.
_OVERRIDE_EVIDENCE_RULE = """
=== Departing from programmatic_stance ===
Measured over 523 recorded departures, this desk's overrides of the
programmatic stance have lost money, and the most expensive pattern was
muting a directional score to neutral. Therefore:
- Default to the programmatic_stance for that product/timeframe.
- To depart from it, add an "override_reason" field to that stance entry
  naming the specific level that justifies the departure, with its price
  (e.g. "H4 order block 63,433-64,188 holding, price in discount").
- An override with no cited price is discarded and the programmatic stance is
  published instead. Do not invent a price to satisfy this rule — keeping the
  programmatic stance is the correct answer when you have no level.
- rationale stays a normal 1-3 sentence read either way.
"""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply.

    The model intermittently wraps the object in a code fence or appends a
    sentence of commentary after it, so decode from the first brace and ignore
    whatever trails the object rather than parsing the whole reply.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in stance reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("stance reply is not a JSON object")
    return obj


def _fallback_stances(
    features: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    stances: list[dict[str, Any]] = []
    for product_id in STANCE_PRODUCTS:
        for tf in STANCE_TIMEFRAMES:
            f = features[product_id][tf]
            stances.append(
                {
                    "product_id": product_id,
                    "timeframe": tf,
                    "stance": f["stance"],
                    # This row *is* the deterministic score, so it is its own
                    # counterfactual and can never read as an override.
                    "det_stance": f["stance"],
                    "confidence": min(abs(f["score"]) / 3.0, 1.0),
                    "rationale": (
                        f"Programmatic: score={f['score']} range_pos={f['range_pos']} "
                        f"HH={f['higher_highs']} LL={f['lower_lows']}"
                    ),
                }
            )
    return stances


_PRICE_RE = re.compile(r"\d[\d,]*\.?\d*")


def _cites_price(text: str | None) -> bool:
    """A cited level needs a number big enough to be a BTC/ETH price.

    Guards against "order block holding" and against a bare "0.618" fib
    passing as a level citation.
    """
    for match in _PRICE_RE.findall(text or ""):
        try:
            value = float(match.replace(",", ""))
        except ValueError:
            continue
        if value >= 100:
            return True
    return False


def apply_override_policy(
    stances: list[dict[str, Any]],
    features: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Attach each row's counterfactual, then enforce the Phase 1 flags.

    With both flags off this only annotates: the published stance is exactly
    what the model returned, and `det_stance` / `override_kind` become the
    standing record the plan's Phase 0 is there to accumulate.
    """
    publish_det = getattr(bot_config, "STANCE_PUBLISH_DETERMINISTIC", False)
    require_evidence = getattr(
        bot_config, "STANCE_OVERRIDE_REQUIRE_EVIDENCE", False
    )

    for entry in stances:
        product_id = str(entry.get("product_id"))
        tf = str(entry.get("timeframe"))
        cell = (features.get(product_id) or {}).get(tf) or {}
        det = cell.get("stance")
        if not det:
            continue
        entry["det_stance"] = det
        if entry.get("stance") == det:
            continue

        attempted = entry["stance"]
        entry["llm_stance"] = attempted
        reason = str(entry.get("override_reason") or "").strip()
        if publish_det:
            discard = "policy: deterministic score published"
        elif require_evidence and not _cites_price(reason):
            discard = "no cited price"
        else:
            continue  # the override stands

        # `llm_stance` keeps the attempt structurally; the text records why it
        # was discarded, which the column cannot say.
        entry["stance"] = det
        # The confidence has to move with the stance. Leaving the model's
        # number on a reverted row publishes its confidence in a *different*
        # call, and downstream gates read that number as conviction in the
        # stance beside it (the Kalshi eva_wick bot thresholds on exactly this
        # field). Same formula the deterministic fallback uses.
        score = cell.get("score")
        if score is not None:
            entry["confidence"] = min(abs(float(score)) / 3.0, 1.0)
        entry["override_reason"] = (
            f"[reverted:{attempted}] {discard}"
            + (f" | model said: {reason}" if reason else "")
        )
        logger.info(
            "Stance override reverted for %s %s: %s -> %s (%s)",
            product_id,
            tf,
            attempted,
            det,
            discard,
        )
    return stances


def _validate_llm_stances(payload: dict) -> list[dict[str, Any]]:
    raw = payload.get("stances")
    if not isinstance(raw, list):
        raise ValueError("stances must be a list")
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "")
        tf = str(item.get("timeframe") or "").upper()
        if product_id not in STANCE_PRODUCTS or tf not in STANCE_TIMEFRAMES:
            continue
        key = (product_id, tf)
        if key in seen:
            continue
        seen.add(key)
        stance = str(item.get("stance") or "neutral").lower()
        if stance not in store.VALID_STANCES:
            stance = "neutral"
        conf = item.get("confidence")
        out.append(
            {
                "product_id": product_id,
                "timeframe": tf,
                "stance": stance,
                "confidence": max(0.0, min(float(conf), 1.0))
                if conf is not None
                else None,
                "rationale": str(item.get("rationale") or "").strip(),
                # Optional; only requested when a Phase 1 flag is on.
                "override_reason": str(item.get("override_reason") or "").strip()
                or None,
            }
        )
    missing = [
        (p, tf)
        for p in STANCE_PRODUCTS
        for tf in STANCE_TIMEFRAMES
        if (p, tf) not in seen
    ]
    if missing:
        raise ValueError(f"missing stance entries: {missing}")
    return out


def run_stance_cycle(cycle_ts: str | None = None) -> dict[str, Any]:
    """Compute and persist the hourly stance batch. Returns the stored payload."""
    cycle_ts = cycle_ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    bars_by_product = gather_bars()
    features = gather_features(bars_by_product)

    macro_block = build_macro_block()
    funding_block = _funding_context_block()

    content_parts = [_STANCE_PROMPT, _features_block(features)]
    if getattr(bot_config, "STANCE_PUBLISH_DETERMINISTIC", False) or getattr(
        bot_config, "STANCE_OVERRIDE_REQUIRE_EVIDENCE", False
    ):
        content_parts.append(_OVERRIDE_EVIDENCE_RULE)
    if funding_block:
        content_parts.append(funding_block)
    if macro_block:
        content_parts.append(macro_block)

    stances: list[dict[str, Any]]
    medium_summary = ""
    btc_eth_note = ""
    source = "llm"
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL_FAST,
            max_tokens=_MAX_STANCE_TOKENS,
            messages=[{"role": "user", "content": "\n\n".join(content_parts)}],
        )
        raw_text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        payload = _extract_json(raw_text)
        stances = _validate_llm_stances(payload)
        medium_summary = str(payload.get("medium_summary") or "").strip()
        btc_eth_note = str(payload.get("btc_eth_note") or "").strip()
    except Exception:
        logger.exception("Stance LLM call failed — using deterministic fallback")
        stances = _fallback_stances(features)
        source = "programmatic"

    if getattr(bot_config, "STANCE_LOG_COUNTERFACTUAL", True):
        stances = apply_override_policy(stances, features)

    store.insert_stances(cycle_ts, stances, source=source)

    # Phase 2 shadow read, on the same bars and the same cycle so the two
    # artifacts see one tape. Wrapped because an experiment must never cost
    # the board its cycle.
    if getattr(bot_config, "INTEL_CONDITIONAL_READS_ENABLED", False):
        try:
            from intelligence import conditional

            conditional.run_conditional_cycle(cycle_ts, bars_by_product)
        except Exception:
            logger.exception("Conditional read cycle failed for %s", cycle_ts)

    render_structure_board(bars_by_product, stances)
    funding_note = funding_block or None
    if not medium_summary:
        btc_h4 = next(
            s for s in stances if s["product_id"] == "BTC-USD" and s["timeframe"] == "H4"
        )
        eth_h4 = next(
            s for s in stances if s["product_id"] == "ETH-USD" and s["timeframe"] == "H4"
        )
        medium_summary = (
            f"Programmatic medium view: BTC H4 {btc_h4['stance']}, "
            f"ETH H4 {eth_h4['stance']}."
        )
    store.insert_medium_summary(
        cycle_ts,
        medium_summary,
        btc_eth_note=btc_eth_note or None,
        funding_note=funding_note,
    )
    overrides = sum(
        1
        for s in stances
        if s.get("det_stance") and s["det_stance"] != s["stance"]
    )
    logger.info(
        "Stance cycle %s: %s override(s) of the deterministic score survived",
        cycle_ts,
        overrides,
    )
    logger.info(
        "Stance cycle %s stored (%s entries, source=%s)",
        cycle_ts,
        len(stances),
        source,
    )
    return {
        "cycle_ts": cycle_ts,
        "stances": stances,
        "medium_summary": medium_summary,
        "btc_eth_note": btc_eth_note,
        "source": source,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_stance_cycle()
    print(json.dumps(result, indent=2))
