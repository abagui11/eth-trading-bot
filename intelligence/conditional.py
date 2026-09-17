"""Phase 2: the conditional ICT read. Shadow artifact — nothing consumes it.

Why this exists
---------------
The scalar stance board measured at coin-flip on its own horizons, but a
scalar was never the ICT claim. A real read is conditional and anchored:
"bullish *while* the discount array holds, invalidated on a close through it,
drawing toward the pool above." The level anchors, the invalidation and the
target are most of the content, and the old schema had nowhere to put them —
so the board's actual thesis has never been testable. This makes it testable.

Detection stays in code
-----------------------
The model is handed *numbered candidates* produced by the existing detectors
(`patterns/htf_structure`, `patterns/fvg`, `patterns/swing`) and returns
indices, not prices. It therefore cannot hallucinate a level: every price on
every stored read came from a detector. That is deliberate — the one paired
LLM-vs-deterministic comparison we have says the model subtracts value when it
supplies structure itself, and the recorded audit verdicts show an M5 order
block cited-but-not-matchable on roughly a cycle in five. The model's job here
is *selection and narrative* on detected structure, which is the remit the
evidence supports.

Reads are written to `intel_reads` and consumed by nobody. `/api/v1` and the
stance board are untouched, so this carries no risk to the mill or the Kalshi
gate and needs no migration. Consumers move only once the nightly scorer
shows conditional accuracy beating the scalar control — see
`deploy/INTEL_BOARD_PLAN.md` Phase 2 for the pre-registered bars.

Conventions we are choosing (ICT does not fix these)
----------------------------------------------------
* invalidation = an M5 **close** through the far boundary of the chosen array.
  Wick-through is the sensitivity, not the default. ICT teaches breaker and
  order-block invalidation by annotated example, not by rule, so this is our
  convention and is stored as `invalidation_trigger` rather than assumed.
* a liquidity pool = >=2 swing extremes within `_POOL_TOL_PCT` of each other
  (equal highs / equal lows). The tolerance is a choice.
* premium/discount = position in the current dealing range, equilibrium being
  the middle `_EQ_BAND` of it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic

import bot_config
import config
import research
from intelligence import store
from patterns.fvg import detect_fvgs
from patterns.htf_structure import detect_htf_zones
from patterns.swing import find_pivots

logger = logging.getLogger(__name__)

_POOL_TOL_PCT = 0.0015     # 15 bps: two extremes this close are "equal"
_EQ_BAND = 0.10            # +/-10% of the range around the midpoint
_MAX_CANDIDATES = 6
_MAX_TOKENS = 1600


def timeframes() -> tuple[str, ...]:
    return tuple(getattr(bot_config, "INTEL_CONDITIONAL_TIMEFRAMES", ("H4", "H1")))


# --------------------------------------------------------------- primitives
def _liquidity_pools(df, price: float) -> list[dict[str, Any]]:
    """Clusters of >=2 equal swing extremes, as unswept liquidity."""
    pivots = find_pivots(df)
    pools: list[dict[str, Any]] = []
    for kind, side in (("high", "buyside_pool"), ("low", "sellside_pool")):
        extremes = sorted(
            (p.price for p in pivots if p.kind == kind), reverse=(kind == "high")
        )
        used: list[float] = []
        for level in extremes:
            if any(abs(level - u) / u <= _POOL_TOL_PCT for u in used if u):
                continue
            touches = [
                e for e in extremes if level and abs(e - level) / level <= _POOL_TOL_PCT
            ]
            if len(touches) < 2:
                continue
            used.append(level)
            lo, hi = min(touches), max(touches)
            # Only pools price has not yet reached can still be a draw.
            if side == "buyside_pool" and hi <= price:
                continue
            if side == "sellside_pool" and lo >= price:
                continue
            pools.append({"kind": side, "lo": lo, "hi": hi, "touches": len(touches)})
    return pools


def _open_gaps(df, price: float) -> list[dict[str, Any]]:
    gaps = detect_fvgs(df, open_only=True)
    return [
        {"kind": "fvg", "lo": g.bottom, "hi": g.top, "side": g.direction,
         "distance_pct": abs(((g.top + g.bottom) / 2) - price) / price * 100}
        for g in gaps
    ]


def _location(df, price: float) -> tuple[str, float, float]:
    """Premium/discount within the current dealing range."""
    window = df.tail(40)
    hi, lo = float(window["high"].max()), float(window["low"].min())
    span = hi - lo
    if span <= 0:
        return "equilibrium", lo, hi
    pos = (price - lo) / span
    if abs(pos - 0.5) <= _EQ_BAND:
        return "equilibrium", lo, hi
    return ("premium" if pos > 0.5 else "discount"), lo, hi


def _array_state(zone, price: float, last_close: float) -> str:
    """holding | traded_through | untested, from the zone's own geometry."""
    if zone.mitigated:
        return "traded_through"
    if zone.direction == "bullish" and last_close < zone.low:
        return "traded_through"
    if zone.direction == "bearish" and last_close > zone.high:
        return "traded_through"
    # "Holding" means price has actually come back and been rejected from it;
    # a zone price has never revisited is context, not support.
    if zone.low <= price <= zone.high:
        return "holding"
    return "untested"


def build_candidates(
    bars_by_product: dict[str, dict[str, list[dict]]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """{(product, timeframe): {attracting: [...], repelling: [...], ...}}.

    Every candidate comes from a detector, so every price the model can pick
    is one the code found.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for product_id, by_tf in bars_by_product.items():
        for tf in timeframes():
            bars = by_tf.get(tf) or []
            if len(bars) < 40:
                continue
            try:
                df = research.to_dataframe(bars)
                price = float(df["close"].iloc[-1])
                last_close = price
                zones = detect_htf_zones(bars, product_id=product_id)
                repelling = [
                    {"kind": z.zone_type, "side": z.direction,
                     "lo": z.low, "hi": z.high,
                     "state": _array_state(z, price, last_close)}
                    for z in zones
                ]
                # Nearest-first: a distant array is not the operative one.
                repelling.sort(key=lambda z: abs((z["lo"] + z["hi"]) / 2 - price))
                attracting = _liquidity_pools(df, price) + _open_gaps(df, price)
                attracting.sort(key=lambda a: abs((a["lo"] + a["hi"]) / 2 - price))
                location, range_lo, range_hi = _location(df, price)
            except Exception:
                logger.exception("Candidate build failed for %s %s", product_id, tf)
                continue
            out[(product_id, tf)] = {
                "price": price,
                "location": location,
                "range_lo": range_lo,
                "range_hi": range_hi,
                "attracting": attracting[:_MAX_CANDIDATES],
                "repelling": repelling[:_MAX_CANDIDATES],
            }
    return out


# ------------------------------------------------------------------- prompt
_PROMPT = """You are the Eva intelligence desk writing a *conditional* ICT read.

You are given detected structure. You do NOT supply prices — you select from
the numbered candidates below by index. Anything you cannot justify from those
candidates you must decline to call.

For each (product, timeframe) return:
- repelling_id: index of the PD array the read is predicated on, or null
- attracting_id: index of the draw on liquidity price is being pulled toward,
  or null
- bias: "bullish" or "bearish", or null
- rationale: 1-2 sentences naming the array and the draw

Rules:
- bias MUST be null unless you select BOTH a repelling array whose state is
  "holding" AND an attracting level. No array holding or no draw identified
  means no bias. Null is a correct, expected answer — it is not a failure.
- The bias must point from the array toward the draw. A bullish bias needs a
  bullish array below price and a draw above it.
- Do not pick an array whose state is "traded_through": the thesis it would
  support is already invalidated.
- Prefer the nearest candidates; they are listed nearest-first.

Return JSON only:
{"reads":[{"product_id":"BTC-USD","timeframe":"H4","repelling_id":0,
"attracting_id":1,"bias":"bullish","rationale":"..."}]}
"""


def _candidates_block(candidates: dict[tuple[str, str], dict[str, Any]]) -> str:
    lines: list[str] = []
    for (product_id, tf), c in candidates.items():
        lines.append(
            f"=== {product_id} {tf} === price={c['price']:,.2f} "
            f"location={c['location']} "
            f"(range {c['range_lo']:,.2f}-{c['range_hi']:,.2f})"
        )
        lines.append("  repelling PD arrays (nearest first):")
        if not c["repelling"]:
            lines.append("    (none detected)")
        for i, z in enumerate(c["repelling"]):
            lines.append(
                f"    [{i}] {z['kind']} {z['side']} {z['lo']:,.2f}-{z['hi']:,.2f} "
                f"state={z['state']}"
            )
        lines.append("  attracting levels (nearest first):")
        if not c["attracting"]:
            lines.append("    (none detected)")
        for i, a in enumerate(c["attracting"]):
            extra = (f" touches={a['touches']}" if "touches" in a
                     else f" side={a.get('side')}")
            lines.append(
                f"    [{i}] {a['kind']} {a['lo']:,.2f}-{a['hi']:,.2f}{extra}"
            )
    return "\n".join(lines)


def _pick(items: list[dict], idx: Any) -> dict | None:
    if idx is None:
        return None
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return None
    return items[i] if 0 <= i < len(items) else None


def _invalidation(repelling: dict | None, bias: str | None) -> float | None:
    """Far boundary of the array the read rests on. Our convention, not ICT's."""
    if not repelling or not bias:
        return None
    return repelling["lo"] if bias == "bullish" else repelling["hi"]


def assemble_read(
    product_id: str,
    tf: str,
    candidate: dict[str, Any],
    choice: dict[str, Any],
) -> dict[str, Any]:
    """Turn an index choice into a stored read, enforcing the null rule in code.

    The model is told the rule; the rule is applied here as well, because a
    bias published against a traded-through array is the exact
    conditional-logic defect this schema exists to catch.
    """
    repelling = _pick(candidate["repelling"], choice.get("repelling_id"))
    attracting = _pick(candidate["attracting"], choice.get("attracting_id"))
    bias = choice.get("bias")
    bias = str(bias).lower() if bias else None
    if bias not in ("bullish", "bearish"):
        bias = None

    dropped = None
    if bias and not (repelling and attracting):
        dropped, bias = "bias without both anchors", None
    elif bias and repelling["state"] != "holding":
        dropped, bias = f"array state={repelling['state']}", None
    elif bias and repelling["side"] != bias:
        dropped, bias = "array side opposes the bias", None
    if dropped:
        logger.info(
            "Conditional bias dropped for %s %s: %s", product_id, tf, dropped
        )

    price = candidate["price"]
    invalidation = _invalidation(repelling, bias)
    # Computed here, never asked of the model: is the thesis already dead at
    # publish time? This is the stale-invalidation metric.
    stale = bool(
        invalidation is not None
        and ((bias == "bullish" and price < invalidation)
             or (bias == "bearish" and price > invalidation))
    )
    return {
        "product_id": product_id,
        "timeframe": tf,
        "bias": bias,
        "attracting_kind": (attracting or {}).get("kind"),
        "attracting_lo": (attracting or {}).get("lo"),
        "attracting_hi": (attracting or {}).get("hi"),
        "repelling_kind": (repelling or {}).get("kind"),
        "repelling_side": (repelling or {}).get("side"),
        "repelling_lo": (repelling or {}).get("lo"),
        "repelling_hi": (repelling or {}).get("hi"),
        "repelling_state": (repelling or {}).get("state"),
        "location": candidate["location"],
        "invalidation_price": invalidation,
        "invalidation_trigger": "m5_close_through",
        "spot": price,
        "rationale": str(choice.get("rationale") or "").strip(),
        "stale_invalidation": stale,
        "dropped_reason": dropped,
    }


def _programmatic_choice(candidate: dict[str, Any]) -> dict[str, Any]:
    """Fallback selection: nearest holding array, nearest draw on its side.

    Keeps the artifact populated when the LLM call fails, and doubles as the
    selection baseline the model has to beat — the same control design the
    stance board should have had from the start.
    """
    holding = [
        (i, z) for i, z in enumerate(candidate["repelling"])
        if z["state"] == "holding"
    ]
    if not holding:
        return {"repelling_id": None, "attracting_id": None, "bias": None,
                "rationale": "No PD array holding."}
    i, zone = holding[0]
    want_above = zone["side"] == "bullish"
    price = candidate["price"]
    draw = next(
        (
            j for j, a in enumerate(candidate["attracting"])
            if (((a["lo"] + a["hi"]) / 2) > price) == want_above
        ),
        None,
    )
    return {
        "repelling_id": i,
        "attracting_id": draw,
        "bias": zone["side"] if draw is not None else None,
        "rationale": (
            f"Programmatic: nearest holding {zone['kind']} "
            f"{zone['lo']:,.2f}-{zone['hi']:,.2f}, "
            + ("draw selected nearest on side." if draw is not None
               else "no draw on that side — bias withheld.")
        ),
    }


def run_conditional_cycle(
    cycle_ts: str | None = None,
    bars_by_product: dict[str, dict[str, list[dict]]] | None = None,
) -> dict[str, Any]:
    """Build candidates, select, persist. Never raises into the caller."""
    cycle_ts = cycle_ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    if bars_by_product is None:
        from intelligence.stance import gather_bars

        bars_by_product = gather_bars()

    candidates = build_candidates(bars_by_product)
    if not candidates:
        logger.warning("Conditional cycle %s: no candidates built", cycle_ts)
        return {"cycle_ts": cycle_ts, "reads": [], "source": "none"}

    source = "llm"
    choices: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL_FAST,
            max_tokens=_MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": _PROMPT + "\n\n" + _candidates_block(candidates),
            }],
        )
        from intelligence.stance import _extract_json

        raw = "".join(b.text for b in response.content if b.type == "text")
        for item in _extract_json(raw).get("reads") or []:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("product_id")), str(item.get("timeframe")).upper())
            if key in candidates:
                choices[key] = item
        if not choices:
            raise ValueError("no usable reads in reply")
    except Exception:
        logger.exception(
            "Conditional read LLM call failed — using programmatic selection"
        )
        choices, source = {}, "programmatic"

    reads = []
    for key, candidate in candidates.items():
        product_id, tf = key
        choice = choices.get(key) or _programmatic_choice(candidate)
        reads.append(assemble_read(product_id, tf, candidate, choice))

    store.insert_reads(cycle_ts, reads, source=source)
    logger.info(
        "Conditional cycle %s: %s reads (%s with a bias, %s stale) source=%s",
        cycle_ts,
        len(reads),
        sum(1 for r in reads if r["bias"]),
        sum(1 for r in reads if r["stale_invalidation"]),
        source,
    )
    return {"cycle_ts": cycle_ts, "reads": reads, "source": source}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run_conditional_cycle(), indent=2, default=str))
