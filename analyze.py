"""Claude vision analysis: charts + Trading Guide -> structured trade suggestion."""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic

import bot_config
import config
from models import Suggestion
import validate
from patterns.market_context import MarketContext
from patterns.order_block import (
    bounds_close,
    entry_valid_at_price,
    fib_zone_bounds,
    find_matching_entry_ob,
    meets_min_ob_width,
    ob_width_pct,
)

logger = logging.getLogger(__name__)

TRADING_GUIDE_PATH = config.TRADING_GUIDE_DIR / "Trading Guide.md"
VALID_ACTIONS = {"spot_buy", "spot_sell", "deriv_buy", "deriv_sell", "no_trade"}
CHART_ORDER = ("H4", "H1", "M5")
MAX_SUGGESTION_TOKENS = 1536
MAX_MULTI_SUGGESTION_TOKENS = 2560
_JSON_RETRY_HINT = (
    "Return valid JSON only. Keep rationale under 400 characters to avoid truncation."
)

# TODO: add critic.py second-pass review before broadcast. (Post-broadcast monitor in critic.py)


def load_trading_guide() -> str:
    if not TRADING_GUIDE_PATH.exists():
        raise FileNotFoundError(f"Trading guide not found: {TRADING_GUIDE_PATH}")
    text = TRADING_GUIDE_PATH.read_text(encoding="utf-8")
    return text.replace("PORTFOLIO_VALUE", str(config.PORTFOLIO_VALUE))

def _encode_image(path: str | Path) -> str:
    return base64.standard_b64encode(Path(path).read_bytes()).decode("utf-8")


def load_pattern_images() -> list[tuple[str, Path]]:
    """All reference PNGs in Trading Guide/ for Claude vision."""
    images: list[tuple[str, Path]] = []
    for path in sorted(config.TRADING_GUIDE_DIR.glob("*.png")):
        label = path.stem.replace("_", " ")
        images.append((f"{label} ({path.name})", path))
    if not images:
        raise FileNotFoundError(f"No pattern images found in {config.TRADING_GUIDE_DIR}")
    return images


def _append_pattern_images(content: list[dict], *, enabled: bool | None = None) -> None:
    """Optionally attach Trading Guide reference PNGs (off by default to save tokens)."""
    if enabled is None:
        enabled = bot_config.INCLUDE_PATTERN_IMAGES
    if not enabled:
        return
    content.append(
        {
            "type": "text",
            "text": "--- Reference pattern examples (match similar structure on live charts) ---",
        }
    )
    for label, path in load_pattern_images():
        content.append({"type": "text", "text": f"--- {label} ---"})
        content.append(_image_block(path))


def log_anthropic_usage(response: object, label: str) -> None:
    """Log input/output/cache token counts when the API returns usage."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    logger.info(
        "anthropic_usage %s input=%s output=%s cache_read=%s cache_create=%s",
        label,
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
        getattr(usage, "cache_read_input_tokens", None),
        getattr(usage, "cache_creation_input_tokens", None),
    )


def _cached_system_blocks(guide_text: str, *, extra_suffix: str = "") -> list[dict]:
    """Trading Guide + overlay legend as one ephemeral-cached system block."""
    text = f"{guide_text}\n\n{OVERLAY_LEGEND}"
    if extra_suffix:
        text = f"{text}\n\n{extra_suffix}"
    return [
        {
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _image_block(path: str | Path) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": _encode_image(path),
        },
    }


OVERLAY_LEGEND = """
## How to read the marked live charts (H4, H1, M5)

Each marked chart is a full-width candlestick image with programmatic overlays. Read overlays directly on the chart — do not infer structure that contradicts what is drawn.

### Key levels (horizontal lines + edge labels)
SpacemanBTC calendar levels from UTC daily Coinbase data. Only levels near the visible price range are drawn.

Each label includes **name and price** (e.g. `Weekly Open 1,569.40`). When levels cluster, labels stagger across **left and right** edges to avoid overlap.

| Color | Labels |
|-------|--------|
| Cyan (#08bcd4) | Daily Open |
| White (#ffffff) | Monday High, Monday Low, Monday Mid |
| Gold (#D4AF37) | Weekly Open, Prev Week High, Prev Week Low, Prev Week Mid |
| Green (#08d48c) | Monthly Open, Prev Month High, Prev Month Low, Prev Month Mid |
| Red (#ff0000) | Quarterly Open, Prev Quarter Mid, Yearly Open, Current Year Mid |

Light-colored labels use dark text on a tinted badge for readability. When two levels share a price, the label merges both names.

### H4 order blocks & breakers (shaded rectangles)
Detected once on **H4** closed candles, then **projected** onto H4, H1, and M5 (same price zone; width maps to nearest bars on each timeframe).

| Visual | Meaning |
|--------|---------|
| Green box, green border, label **H4 OB** | Bullish order block — last bearish H4 candle before a bullish MSB (close broke above prior swing high) |
| Pink box, red border, label **H4 OB** | Bearish order block — last bullish H4 candle before a bearish MSB (close broke below prior swing low) |
| Green box, label **H4 BRKR** | Bullish breaker — a **mitigated bearish OB** reclassified after a subsequent bullish MSB |
| Pink/red box, label **H4 BRKR** | Bearish breaker — a **mitigated bullish OB** reclassified after a subsequent bearish MSB |
| Faint horizontal line inside box | Zone midpoint |
| Box ends before the right edge | Zone was **mitigated** (close traded through the block) |
| Box extends to the right edge | Zone is still **active** (not yet mitigated) |

MSB uses **close only** — wick-only breaks through a swing do not count.

### M5 order blocks (LTF — entries only)
Detected separately on **M5** candles. On the **M5 marked chart**, valid blocks appear as labeled green/pink rectangles (**M5 OB**). These are **not** the same as H4 OB boxes unless price zones genuinely overlap.

| Rule | Detail |
|------|--------|
| **order_block JSON** | Must be an **M5 OB** (candle timestamps on the M5 chart). Never copy H4 OB bounds into order_block. |
| **Minimum width** | M5 OB zone must be at least **0.15%** wide (high−low as % of mid price). H4 HTF OBs still use the wider **1.25%** filter. |
| **Entry** | Must sit on an M5 OB fib tranche (**0.25** or **0.50**) or inside the **0.25–0.50** band (see programmatic context). Scale-in at **0.718** is watchdog-only. |
| **Rationale** | Cite **H4 OB/BRKR** for HTF context; cite **M5 OB** for entry justification. HTF bias is advisory — do not skip a valid M5 OB/SFP trade solely because H4 has not flipped. If zones overlap, say "M5 OB coincides with H4 OB". |
| **No M5 fib** | If price is only inside an H4 OB (not M5 fib), return **no_trade** or wait for M5 retest. |

### Other overlays
- **Gray dashed lines**: recent swing high and swing low on that chart's timeframe (reference only).
- Programmatic context text may list nearest levels and H4 zones — **always verify on the chart image**.

Cite specific visible level names and H4 OB/BRKR zones in your rationale (ICT-style narration).
"""


def _build_user_content(
    chart_paths: dict[str, str],
    market_context: MarketContext | None = None,
    audit_feedback: str | None = None,
    product_id: str | None = None,
) -> list[dict]:
    product_id = product_id or bot_config.DEFAULT_PRODUCT_ID
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Analyze live {product_id} marked charts and apply the Trading Guide strategy. "
                "Cite H4 OB/BRKR for HTF context and M5 OB (with fib zone) for entries — "
                "never label an H4 box as 'M5 OB'. HTF is advisory, not a hard veto on M5 setups. "
                "Structure rationale as short paragraphs (HTF structure, H4 supply/demand, "
                "LTF/M5 OB context, trade decision) separated by blank lines. "
                "If the trade action conflicts with programmatic market context "
                "(e.g. short while price is inside a bullish M5 OB, or long against a primary "
                "bearish H4 zone), briefly say why you still take it (M5 trigger precedence / "
                "HTF advisory only). "
                "Include macro_note (string) when Macro context is present — acknowledge "
                "active headlines or state they are not material. "
                "Return one JSON trade suggestion. JSON only."
            ),
        },
    ]
    if audit_feedback:
        content.append(
            {
                "type": "text",
                "text": (
                    "Your prior rationale failed fact-check. Fix these errors; cite ONLY "
                    "structures listed in programmatic context. Do not invent M5 OB ranges "
                    "or cite invalidated SFPs.\n\n"
                    f"{audit_feedback}"
                ),
            }
        )
    if market_context and market_context.summary_text:
        content.append(
            {
                "type": "text",
                "text": market_context.to_prompt_block(),
            }
        )
    for tf in CHART_ORDER:
        path = chart_paths[tf]
        content.append({"type": "text", "text": f"--- Live {tf} marked chart ---"})
        content.append(_image_block(path))

    _append_pattern_images(content)
    return content


def _build_multi_user_content(
    charts_by_product: dict[str, dict[str, str]],
    contexts_by_product: dict[str, MarketContext],
    relative_strength: str,
    ratio_chart_path: str | Path | None = None,
    audit_feedback: str | None = None,
) -> list[dict]:
    """Build one vision request containing both assets and ETH/BTC context."""
    products = [pid for pid in bot_config.TRADED_PRODUCTS if pid in charts_by_product]
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Analyze both ETH-USD and BTC-USD. Lead with why ETH versus BTC should "
                "receive more, less, or equal weight based on the W1 ETH/BTC ratio. You "
                "may return 0-2 actionable trades, at most one per product; concurrent "
                "ETH and BTC trades are allowed. Every rationale must cite the ETH/BTC "
                "bias and explain how it affects that asset. Apply the same H4/H1/M5 ICT "
                "rules and M5 order-block validation to each product independently.\n\n"
                "Return JSON only in this shape:\n"
                '{"asset_preference":"brief ETH/BTC weighting reason","trades":['
                '{"product_id":"ETH-USD","action":"spot_buy",'
                '"size":0,"entry":0,"stop_loss":0,"take_profits":[],'
                '"risk_reward":null,"rationale":"...",'
                '"macro_note":"1-2 sentences on active macro (or none material)",'
                '"decision_charts":["H4","H1","M5"],'
                '"structure_chart":"H4","entry_chart":"M5","order_block":null},'
                '{"product_id":"BTC-USD","action":"no_trade","rationale":"...",'
                '"macro_note":"none material"}'
                "]}\n"
                "When Macro context appears in a product block, macro_note is required "
                "(acknowledge bias/severity or say macro is not material for this setup). "
                "The size field is USD notional, not ETH/BTC quantity. It may be 0; "
                "validation will overwrite it with the configured dollar deployment."
            ),
        },
        {
            "type": "text",
            "text": f"=== Authoritative relative-strength context ===\n{relative_strength}",
        },
    ]
    if audit_feedback:
        content.append(
            {
                "type": "text",
                "text": (
                    "Correct these prior fact-check issues and cite only structures in "
                    f"the matching product context:\n\n{audit_feedback}"
                ),
            }
        )
    if ratio_chart_path:
        content.append({"type": "text", "text": "--- ETH/BTC W1 ratio chart ---"})
        content.append(_image_block(ratio_chart_path))

    for product_id in products:
        context = contexts_by_product.get(product_id)
        content.append(
            {
                "type": "text",
                "text": (
                    f"=== {product_id} market context ===\n"
                    f"{context.to_prompt_block() if context else 'Unavailable'}"
                ),
            }
        )
        for tf in CHART_ORDER:
            path = charts_by_product[product_id][tf]
            content.append(
                {"type": "text", "text": f"--- {product_id} live {tf} marked chart ---"}
            )
            content.append(_image_block(path))

    _append_pattern_images(content)
    return content


def build_vision_content(
    chart_paths: dict[str, str] | None = None,
    annotated_h1_path: str | Path | None = None,
    include_live_charts: bool = True,
    include_patterns: bool | None = None,
) -> list[dict]:
    """Build Claude vision content blocks for analyze or chat."""
    content: list[dict] = []
    if include_patterns is None:
        include_patterns = bot_config.INCLUDE_PATTERN_IMAGES

    if include_live_charts and chart_paths:
        for tf in CHART_ORDER:
            path = chart_paths.get(tf)
            if not path:
                continue
            content.append({"type": "text", "text": f"--- Live {tf} chart ---"})
            content.append(_image_block(path))

    if annotated_h1_path:
        content.append({"type": "text", "text": "--- Latest annotated M5 suggestion chart ---"})
        content.append(_image_block(annotated_h1_path))

    _append_pattern_images(content, enabled=include_patterns)

    return content


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _validate_chart_fields(suggestion: Suggestion) -> None:
    valid = set(CHART_ORDER)
    if suggestion.action == "no_trade":
        if not suggestion.decision_charts:
            suggestion.decision_charts = ["H4"]
        invalid = [c for c in suggestion.decision_charts if c not in valid]
        if invalid:
            raise ValueError(f"Invalid decision_charts: {invalid}")
        return

    if not suggestion.entry_chart or suggestion.entry_chart not in valid:
        suggestion.entry_chart = "M5"
    if not suggestion.structure_chart or suggestion.structure_chart not in valid:
        suggestion.structure_chart = "H4"
    for field_name in ("structure_chart", "entry_chart"):
        val = getattr(suggestion, field_name)
        if not val or val not in valid:
            raise ValueError(f"Missing or invalid {field_name}")
    if suggestion.decision_charts:
        suggestion.decision_charts = [c for c in suggestion.decision_charts if c in valid]


def _trade_direction(action: str) -> str:
    if action in ("spot_buy", "deriv_buy"):
        return "bullish"
    if action in ("spot_sell", "deriv_sell"):
        return "bearish"
    raise ValueError(f"Cannot infer OB direction for action: {action}")


def _validate_order_block_entry(
    suggestion: Suggestion,
    market_context: MarketContext | None,
) -> None:
    """Ensure order_block is a real M5 OB and entry sits in the fib sweet spot."""
    ob = suggestion.order_block
    assert ob is not None
    low = float(ob["low"])
    high = float(ob["high"])
    if low >= high:
        raise ValueError(f"order_block low ({low}) must be below high ({high})")
    if not meets_min_ob_width(low, high, min_width_pct=bot_config.OB_MIN_WIDTH_PCT_M5):
        raise ValueError(
            f"order_block width ({ob_width_pct(low, high):.2f}%) is below minimum "
            f"{bot_config.OB_MIN_WIDTH_PCT_M5:.2f}%"
        )

    direction = _trade_direction(suggestion.action)
    entry = float(suggestion.entry)  # type: ignore[arg-type]
    z_low, z_high = fib_zone_bounds(direction, low, high)

    if not entry_valid_at_price(entry, direction, low, high):
        raise ValueError(
            f"entry {entry:,.2f} outside M5 OB fib entry band "
            f"({z_low:,.2f}-{z_high:,.2f}) or tranche levels 0.25/0.50 "
            f"for order_block {low:,.2f}-{high:,.2f}"
        )

    if market_context is None:
        return

    m5_obs = market_context.order_blocks
    if not m5_obs:
        return

    match = find_matching_entry_ob(ob, m5_obs, direction)  # type: ignore[arg-type]
    if match is not None:
        return

    htf_obs = [
        z
        for z in market_context.htf_zones
        if z.zone_type == "order_block" and not z.mitigated
    ]
    h4_by_ts = next(
        (
            z
            for z in htf_obs
            if z.direction == direction and ob.get("start_ts") == z.start_ts
        ),
        None,
    )
    h4_by_bounds = next(
        (
            z
            for z in htf_obs
            if z.direction == direction and bounds_close(low, high, z.low, z.high)
        ),
        None,
    )
    h4_overlap = h4_by_ts or h4_by_bounds
    if h4_overlap is not None:
        raise ValueError(
            f"order_block {low:,.2f}-{high:,.2f} matches H4 OB "
            f"({h4_overlap.low:,.2f}-{h4_overlap.high:,.2f}) but not any detected M5 OB — "
            "use M5 OB bounds in order_block JSON or return no_trade"
        )
    raise ValueError(
        "order_block does not match any detected M5 OB — verify on M5 chart or return no_trade"
    )


def _validate(
    data: dict,
    market_context: MarketContext | None = None,
    *,
    spots: dict[str, float] | None = None,
) -> Suggestion:
    action = str(data.get("action", "no_trade"))
    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid action: {action}")

    suggestion = Suggestion.from_dict(data)
    _validate_chart_fields(suggestion)

    if action == "no_trade":
        return suggestion

    for field_name in ("entry", "stop_loss"):
        val = getattr(suggestion, field_name)
        if val is None or not isinstance(val, (int, float)):
            raise ValueError(f"Missing or invalid {field_name}")

    if not suggestion.take_profits:
        raise ValueError("take_profits required for trade actions")

    if suggestion.order_block is None:
        raise ValueError("order_block required for chart markup")

    ob = suggestion.order_block
    for key in ("low", "high", "start_ts", "end_ts"):
        if key not in ob:
            raise ValueError(f"order_block missing {key}")

    _validate_order_block_entry(suggestion, market_context)
    spot = market_context.spot if market_context is not None else None
    validate.validate_trade_risk(suggestion, spot_price=spot, spots=spots)

    return suggestion


def validate_suggestion(
    data: dict,
    market_context: MarketContext | None = None,
    *,
    spots: dict[str, float] | None = None,
) -> Suggestion:
    """Public wrapper for programmatic / watchdog trade validation."""
    return _validate(data, market_context=market_context, spots=spots)


def propose_trade(
    chart_paths: dict[str, str],
    trading_guide: str | None = None,
    market_context: MarketContext | None = None,
    audit_feedback: str | None = None,
    product_id: str | None = None,
) -> Suggestion:
    """Single Claude call: chart images + Trading Guide -> Suggestion (or no_trade on failure)."""
    product_id = product_id or bot_config.DEFAULT_PRODUCT_ID
    guide_text = trading_guide if trading_guide is not None else load_trading_guide()
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    feedback = audit_feedback
    last_exc: Exception | None = None

    for attempt in range(2):
        try:
            response = client.messages.create(
                model=config.ANTHROPIC_MODEL,
                max_tokens=MAX_SUGGESTION_TOKENS,
                system=_cached_system_blocks(guide_text),
                messages=[
                    {
                        "role": "user",
                        "content": _build_user_content(
                            chart_paths,
                            market_context,
                            audit_feedback=feedback,
                            product_id=product_id,
                        ),
                    }
                ],
            )
            log_anthropic_usage(response, f"propose_trade:{product_id}")
        except Exception as exc:
            logger.exception("Claude API call failed")
            return Suggestion.no_trade(f"api_error: {exc}", product_id=product_id)

        raw_text = ""
        for block in response.content:
            if block.type == "text":
                raw_text += block.text

        try:
            data = _extract_json(raw_text)
            data["product_id"] = product_id
            return _validate(data, market_context=market_context)
        except json.JSONDecodeError as exc:
            last_exc = exc
            logger.warning("Malformed JSON suggestion: %s | raw=%s", exc, raw_text[:500])
            if attempt == 0:
                feedback = f"{feedback}\n\n{_JSON_RETRY_HINT}" if feedback else _JSON_RETRY_HINT
                continue
            return Suggestion.no_trade(f"parse_error: {exc}", product_id=product_id)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Malformed suggestion: %s | raw=%s", exc, raw_text[:500])
            return Suggestion.no_trade(f"parse_error: {exc}", product_id=product_id)

    return Suggestion.no_trade(f"parse_error: {last_exc}", product_id=product_id)


def propose_trades_multi(
    charts_by_product: dict[str, dict[str, str]],
    contexts_by_product: dict[str, MarketContext],
    relative_strength: str,
    ratio_chart_path: str | Path | None = None,
    trading_guide: str | None = None,
    audit_feedback: str | None = None,
) -> list[Suggestion]:
    """One Claude vision call for independent ETH and BTC trade decisions."""
    guide_text = trading_guide if trading_guide is not None else load_trading_guide()
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    feedback = audit_feedback
    allowed_products = set(charts_by_product) & set(contexts_by_product)

    for attempt in range(2):
        try:
            response = client.messages.create(
                model=config.ANTHROPIC_MODEL,
                max_tokens=MAX_MULTI_SUGGESTION_TOKENS,
                system=_cached_system_blocks(guide_text),
                messages=[
                    {
                        "role": "user",
                        "content": _build_multi_user_content(
                            charts_by_product,
                            contexts_by_product,
                            relative_strength,
                            ratio_chart_path=ratio_chart_path,
                            audit_feedback=feedback,
                        ),
                    }
                ],
            )
            log_anthropic_usage(response, "propose_trades_multi")
        except Exception as exc:
            logger.exception("Claude multi-asset API call failed")
            return [
                Suggestion.no_trade(f"api_error: {exc}", product_id=pid)
                for pid in bot_config.TRADED_PRODUCTS
                if pid in allowed_products
            ]

        raw_text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        try:
            payload = _extract_json(raw_text)
            asset_preference = str(payload.get("asset_preference") or "").strip()
            raw_trades = payload.get("trades")
            if not isinstance(raw_trades, list):
                raise ValueError("trades must be a JSON array")
            if len(raw_trades) > len(allowed_products):
                raise ValueError("at most one decision per product is allowed")

            import research

            spots = research.get_spot_prices(list(allowed_products))
            suggestions: list[Suggestion] = []
            seen: set[str] = set()
            for raw_trade in raw_trades:
                if not isinstance(raw_trade, dict):
                    raise ValueError("each trade must be a JSON object")
                product_id = str(raw_trade.get("product_id") or "")
                if product_id not in allowed_products:
                    raise ValueError(f"unsupported product_id: {product_id}")
                if product_id in seen:
                    raise ValueError(f"duplicate product_id: {product_id}")
                seen.add(product_id)
                trade_data = dict(raw_trade)
                trade_data["product_id"] = product_id
                rationale = str(trade_data.get("rationale") or "").strip()
                if asset_preference and asset_preference.lower() not in rationale.lower():
                    trade_data["rationale"] = (
                        f"Asset preference: {asset_preference}\n\n{rationale}".strip()
                    )
                try:
                    suggestion = _validate(
                        trade_data,
                        market_context=contexts_by_product[product_id],
                        spots=spots,
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning(
                        "Rejected %s multi-asset decision: %s",
                        product_id,
                        exc,
                    )
                    rationale = f"parse_error: {exc}"
                    if asset_preference:
                        rationale = (
                            f"Asset preference: {asset_preference}\n\n{rationale}"
                        )
                    suggestion = Suggestion.no_trade(
                        rationale,
                        product_id=product_id,
                    )
                suggestions.append(suggestion)
            return suggestions
        except json.JSONDecodeError as exc:
            logger.warning("Malformed multi-asset JSON: %s | raw=%s", exc, raw_text[:500])
            if attempt == 0:
                feedback = f"{feedback}\n\n{_JSON_RETRY_HINT}" if feedback else _JSON_RETRY_HINT
                continue
            return []
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Malformed multi-asset suggestion: %s | raw=%s", exc, raw_text[:500])
            return []

    return []


if __name__ == "__main__":
    import research
    from charts import build_output_charts, render_marked_charts
    from patterns.htf_structure import detect_htf_zones
    from patterns.key_levels import compute_key_levels
    from patterns.market_context import build_market_context

    logging.basicConfig(level=logging.INFO)
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print("Fetching OHLC and rendering marked charts...")
    data = research.get_all_timeframes()
    daily = research.get_daily_bars_for_levels()
    key_levels = compute_key_levels(daily)
    htf_zones = detect_htf_zones(data["H4"])
    ctx = build_market_context(data["H4"], data["H1"], data["M5"], daily_bars=daily)
    paths = render_marked_charts(
        data, key_levels, htf_zones, cycle_id=cycle_id, market_context=ctx
    )

    print("Calling Claude...")
    suggestion = propose_trade(paths, market_context=ctx)
    print(f"action={suggestion.action}")
    print(f"entry={suggestion.entry} sl={suggestion.stop_loss} tps={suggestion.take_profits}")
    print(f"rr={suggestion.risk_reward}")
    print(f"decision_charts={suggestion.decision_charts}")
    print(f"structure={suggestion.structure_chart} entry_tf={suggestion.entry_chart}")
    print(f"rationale={suggestion.rationale}")

    print("\nBuilding output charts...")
    outputs = build_output_charts(suggestion, data, key_levels, htf_zones, cycle_id, ctx)
    for path in outputs:
        print(f"  {path}")
