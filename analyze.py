"""Claude vision analysis: charts + Trading Guide -> structured trade suggestion."""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic

import config
from models import Suggestion
from patterns.market_context import MarketContext

logger = logging.getLogger(__name__)

TRADING_GUIDE_PATH = config.TRADING_GUIDE_DIR / "Trading Guide.md"
VALID_ACTIONS = {"spot_buy", "spot_sell", "deriv_buy", "deriv_sell", "no_trade"}
CHART_ORDER = ("H12", "H4", "H1")

# TODO: add critic.py second-pass review before broadcast.


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
## How to read the marked live charts (H12, H4, H1)

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

### H12 order blocks & breakers (shaded rectangles)
Detected once on **H12** closed candles, then **projected** onto H12, H4, and H1 (same price zone; width maps to nearest bars on each timeframe).

| Visual | Meaning |
|--------|---------|
| Green box, green border, label **H12 OB** | Bullish order block — last bearish H12 candle before a bullish MSB (close broke above prior swing high) |
| Pink box, red border, label **H12 OB** | Bearish order block — last bullish H12 candle before a bearish MSB (close broke below prior swing low) |
| Green box, label **H12 BRKR** | Bullish breaker — a **mitigated bearish OB** reclassified after a subsequent bullish MSB |
| Pink/red box, label **H12 BRKR** | Bearish breaker — a **mitigated bullish OB** reclassified after a subsequent bearish MSB |
| Faint horizontal line inside box | Zone midpoint |
| Box ends before the right edge | Zone was **mitigated** (close traded through the block) |
| Box extends to the right edge | Zone is still **active** (not yet mitigated) |

MSB uses **close only** — wick-only breaks through a swing do not count.

### Other overlays
- **Gray dashed lines**: recent swing high and swing low on that chart's timeframe (reference only).
- Programmatic context text may list nearest levels and H12 zones — **always verify on the chart image**.

Cite specific visible level names and H12 OB/BRKR zones in your rationale (ICT-style narration).
"""


def _build_user_content(
    chart_paths: dict[str, str],
    market_context: MarketContext | None = None,
) -> list[dict]:
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Analyze live ETH-USD marked charts and apply the Trading Guide strategy. "
                "Compare live structure to all reference pattern images below. "
                "Cite visible key levels and H12 OB/BRKR boxes in your rationale. "
                "Return one JSON trade suggestion. JSON only."
            ),
        },
        {"type": "text", "text": OVERLAY_LEGEND},
    ]
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

    content.append(
        {
            "type": "text",
            "text": "--- Reference pattern examples (match similar structure on live charts) ---",
        }
    )
    for label, path in load_pattern_images():
        content.append({"type": "text", "text": f"--- {label} ---"})
        content.append(_image_block(path))

    return content


def build_vision_content(
    chart_paths: dict[str, str] | None = None,
    annotated_h1_path: str | Path | None = None,
    include_live_charts: bool = True,
    include_patterns: bool = True,
) -> list[dict]:
    """Build Claude vision content blocks for analyze or chat."""
    content: list[dict] = []

    if include_live_charts and chart_paths:
        for tf in CHART_ORDER:
            content.append({"type": "text", "text": f"--- Live {tf} chart ---"})
            content.append(_image_block(chart_paths[tf]))

    if annotated_h1_path:
        content.append({"type": "text", "text": "--- Latest annotated H1 suggestion chart ---"})
        content.append(_image_block(annotated_h1_path))

    if include_patterns:
        content.append(
            {
                "type": "text",
                "text": "--- Reference pattern examples ---",
            }
        )
        for label, path in load_pattern_images():
            content.append({"type": "text", "text": f"--- {label} ---"})
            content.append(_image_block(path))

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
            suggestion.decision_charts = ["H12"]
        invalid = [c for c in suggestion.decision_charts if c not in valid]
        if invalid:
            raise ValueError(f"Invalid decision_charts: {invalid}")
        return

    if not suggestion.entry_chart or suggestion.entry_chart not in valid:
        suggestion.entry_chart = "H1"
    if not suggestion.structure_chart or suggestion.structure_chart not in valid:
        suggestion.structure_chart = "H12"
    for field_name in ("structure_chart", "entry_chart"):
        val = getattr(suggestion, field_name)
        if not val or val not in valid:
            raise ValueError(f"Missing or invalid {field_name}")
    if suggestion.decision_charts:
        suggestion.decision_charts = [c for c in suggestion.decision_charts if c in valid]


def _validate(data: dict) -> Suggestion:
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

    if suggestion.risk_reward is not None and suggestion.risk_reward < 1.5:
        raise ValueError(f"R/R {suggestion.risk_reward} below 1.5 gate")

    if suggestion.order_block is None:
        raise ValueError("order_block required for chart markup")

    ob = suggestion.order_block
    for key in ("low", "high", "start_ts", "end_ts"):
        if key not in ob:
            raise ValueError(f"order_block missing {key}")

    return suggestion


def propose_trade(
    chart_paths: dict[str, str],
    trading_guide: str | None = None,
    market_context: MarketContext | None = None,
) -> Suggestion:
    """Single Claude call: chart images + Trading Guide -> Suggestion (or no_trade on failure)."""
    guide_text = trading_guide if trading_guide is not None else load_trading_guide()
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": guide_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": _build_user_content(chart_paths, market_context),
                }
            ],
        )
    except Exception as exc:
        logger.exception("Claude API call failed")
        return Suggestion.no_trade(f"api_error: {exc}")

    raw_text = ""
    for block in response.content:
        if block.type == "text":
            raw_text += block.text

    try:
        data = _extract_json(raw_text)
        return _validate(data)
    except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Malformed suggestion: %s | raw=%s", exc, raw_text[:500])
        return Suggestion.no_trade(f"parse_error: {exc}")


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
    htf_zones = detect_htf_zones(data["H12"])
    paths = render_marked_charts(data, key_levels, htf_zones, cycle_id=cycle_id)
    ctx = build_market_context(data["H12"], data["H4"], data["H1"], daily_bars=daily)

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
