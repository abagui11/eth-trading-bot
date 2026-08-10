"""Long-horizon (4-year cycle) thesis: BTC-first, ETH as directional conduit.

Daily job:
1. Fetch full BTC-USD daily history (Coinbase, paginated).
2. Render one annotated log-scale chart with halving markers, prior cycle
   analogs, and the current phase label.
3. Compute gold-ratio nuance (BTC/gold, ETH/gold via PAXG-USD) — context only.
4. Ask Claude (fast model) for a thesis JSON; deterministic fallback keeps the
   artifact fresh even when the LLM call fails.

The long thesis never auto-mints trade cards — it is a posture artifact for
the dashboard and the yield book.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timezone
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import anthropic

import config
import research
from intelligence import store

logger = logging.getLogger(__name__)

# Bitcoin halving dates (UTC). The next expected halving anchors the cycle clock.
HALVINGS: tuple[str, ...] = (
    "2012-11-28",
    "2016-07-09",
    "2020-05-11",
    "2024-04-20",
)
NEXT_HALVING_EST = "2028-04-15"

# Rough historical cycle anatomy in days after a halving (advisory analogs).
PHASE_BOUNDS: tuple[tuple[int, str], ...] = (
    (0, "post_halving_accumulation"),      # 0-180d: chop/accumulation
    (180, "bull_expansion"),               # 180-550d: historical bull leg
    (550, "cycle_top_window"),             # 550-750d: prior tops printed here
    (750, "bear_drawdown"),                # 750-1100d: historical bear
    (1100, "pre_halving_accumulation"),    # 1100d+: basing into next halving
)

_MAX_THESIS_TOKENS = 900
_GOLD_PRODUCT = "PAXG-USD"  # on-chain gold proxy on Coinbase


def current_phase(as_of: date | None = None) -> tuple[str, int]:
    """(phase_label, days_since_last_halving) for the current cycle position."""
    today = as_of or datetime.now(timezone.utc).date()
    last_halving = max(
        (date.fromisoformat(h) for h in HALVINGS if date.fromisoformat(h) <= today),
        default=date.fromisoformat(HALVINGS[0]),
    )
    days_since = (today - last_halving).days
    label = PHASE_BOUNDS[0][1]
    for threshold, phase in PHASE_BOUNDS:
        if days_since >= threshold:
            label = phase
    return label, days_since


def fetch_btc_history(*, years: int = 10) -> list[dict[str, Any]]:
    """Paginated BTC-USD daily candles back `years` years."""
    end = int(time.time())
    start = end - years * 365 * 86400
    return research.fetch_coinbase_candles_range(
        "ONE_DAY", start, end, product_id="BTC-USD"
    )


def compute_gold_ratios() -> dict[str, Any]:
    """BTC/gold and ETH/gold spot ratios + 90d trend (nuance panel, not a signal)."""
    out: dict[str, Any] = {}
    try:
        gold_bars = research.get_ohlc("D1", limit=120, product_id=_GOLD_PRODUCT)
    except Exception:
        logger.exception("Gold proxy fetch failed (%s)", _GOLD_PRODUCT)
        return {"available": False}

    gold_by_ts = {str(b["ts"]): float(b["close"]) for b in gold_bars}
    for product_id, key in (("BTC-USD", "btc_gold"), ("ETH-USD", "eth_gold")):
        try:
            bars = research.get_ohlc("D1", limit=120, product_id=product_id)
        except Exception:
            logger.exception("Daily fetch failed for %s", product_id)
            continue
        ratios: list[tuple[str, float]] = []
        for b in bars:
            ts = str(b["ts"])
            gold = gold_by_ts.get(ts)
            if gold and gold > 0:
                ratios.append((ts, float(b["close"]) / gold))
        if len(ratios) < 2:
            continue
        latest = ratios[-1][1]
        base_idx = max(len(ratios) - 91, 0)
        base = ratios[base_idx][1]
        out[key] = {
            "latest": round(latest, 4),
            "change_90d_pct": round((latest - base) / base * 100.0, 2)
            if base > 0
            else None,
        }
    out["available"] = bool(out.get("btc_gold") or out.get("eth_gold"))
    return out


def render_cycle_chart(
    bars: list[dict[str, Any]],
    *,
    phase: str,
    days_since_halving: int,
) -> str:
    """One annotated log-scale BTC chart with halving markers. Returns PNG path."""
    df = pd.DataFrame(bars)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts")

    fig, ax = plt.subplots(figsize=(18, 8), dpi=120)
    ax.plot(df.index, df["close"], linewidth=1.2, color="#f7931a", label="BTC-USD (daily close)")
    ax.set_yscale("log")

    for h in HALVINGS:
        h_dt = pd.Timestamp(h, tz="UTC")
        if h_dt < df.index[0] or h_dt > df.index[-1]:
            continue
        ax.axvline(h_dt, color="#4c9be8", linestyle="--", linewidth=1.0, alpha=0.9)
        ax.annotate(
            f"Halving {h}",
            xy=(h_dt, df["close"].min()),
            xytext=(6, 12),
            textcoords="offset points",
            rotation=90,
            fontsize=9,
            color="#4c9be8",
        )

    next_h = pd.Timestamp(NEXT_HALVING_EST, tz="UTC")
    if next_h > df.index[-1]:
        ax.axvline(
            df.index[-1], color="#888888", linestyle=":", linewidth=0.8, alpha=0.5
        )

    title = (
        f"BTC 4-year cycle — phase: {phase.replace('_', ' ')} "
        f"({days_since_halving}d since halving) | next halving est {NEXT_HALVING_EST}"
    )
    ax.set_title(title, fontsize=13)
    ax.set_ylabel("USD (log)")
    ax.grid(True, which="both", linestyle=":", alpha=0.4)
    ax.legend(loc="upper left", fontsize=9)
    fig.autofmt_xdate()

    config.CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = config.CHARTS_DIR / f"{stamp}_btc_cycle.png"
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return str(path)


_THESIS_PROMPT = """You are the Republic Technologies long-horizon desk. Write the
four-year-cycle thesis. BTC leads the cycle; ETH is a higher-beta directional
conduit that follows BTC with lag and amplitude. Gold ratios are nuance, not a
signal. Horizon: months to years — this is NOT a trade call.

Return JSON only:
{"bias":"bullish|neutral|bearish",
"btc_thesis":"3-5 sentences: where we are in the 4y cycle and what history implies",
"eth_conduit":"2-3 sentences: what the BTC cycle phase implies for ETH",
"gold_note":"1-2 sentences on BTC/gold and ETH/gold context",
"risks":"1-2 sentences on what invalidates this",
"confidence":0.0}
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _fallback_thesis(
    phase: str,
    days_since: int,
    gold: dict[str, Any],
) -> dict[str, Any]:
    bias_by_phase = {
        "post_halving_accumulation": "neutral",
        "bull_expansion": "bullish",
        "cycle_top_window": "neutral",
        "bear_drawdown": "bearish",
        "pre_halving_accumulation": "neutral",
    }
    return {
        "bias": bias_by_phase.get(phase, "neutral"),
        "btc_thesis": (
            f"Programmatic cycle read: {days_since} days since the last halving "
            f"places BTC in the {phase.replace('_', ' ')} window by prior-cycle analogs."
        ),
        "eth_conduit": (
            "ETH historically follows BTC direction with higher beta and lag; "
            "treat the BTC phase as the directional conduit."
        ),
        "gold_note": json.dumps(gold) if gold.get("available") else "Gold ratio unavailable.",
        "risks": "Cycle analogs are advisory; structural regime changes invalidate them.",
        "confidence": 0.3,
    }


def build_thesis(
    phase: str,
    days_since: int,
    gold: dict[str, Any],
    *,
    spot_summary: str,
) -> dict[str, Any]:
    """LLM thesis with deterministic fallback."""
    context = (
        f"Cycle phase: {phase} ({days_since} days since last halving; "
        f"halvings: {', '.join(HALVINGS)}; next est {NEXT_HALVING_EST}).\n"
        f"{spot_summary}\n"
        f"Gold ratios: {json.dumps(gold)}"
    )
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL_FAST,
            max_tokens=_MAX_THESIS_TOKENS,
            messages=[
                {"role": "user", "content": f"{_THESIS_PROMPT}\n\n{context}"}
            ],
        )
        raw = "".join(b.text for b in response.content if b.type == "text")
        thesis = _extract_json(raw)
        if str(thesis.get("bias")) not in ("bullish", "neutral", "bearish"):
            raise ValueError("invalid bias")
        return thesis
    except Exception:
        logger.exception("Long thesis LLM call failed — using fallback")
        return _fallback_thesis(phase, days_since, gold)


def run_long_thesis_refresh() -> dict[str, Any]:
    """Refresh the daily long thesis artifact. Idempotent per calendar day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = store.latest_long_thesis()
    if existing and str(existing.get("as_of_date")) == today:
        logger.info("Long thesis already fresh for %s", today)
        return existing

    phase, days_since = current_phase()
    bars = fetch_btc_history(years=10)
    chart_path = render_cycle_chart(
        bars, phase=phase, days_since_halving=days_since
    )
    gold = compute_gold_ratios()

    last_close = float(bars[-1]["close"]) if bars else 0.0
    year_ago_close = (
        float(bars[-365]["close"]) if len(bars) > 365 else float(bars[0]["close"])
    )
    spot_summary = (
        f"BTC spot ~${last_close:,.0f}; 1y change "
        f"{(last_close - year_ago_close) / year_ago_close * 100.0:+.1f}%."
        if bars
        else "BTC spot unavailable."
    )

    thesis = build_thesis(phase, days_since, gold, spot_summary=spot_summary)
    thesis["days_since_halving"] = days_since
    thesis["gold_ratios"] = gold
    store.insert_long_thesis(today, phase, thesis, chart_path=chart_path)
    logger.info("Long thesis stored for %s (phase=%s)", today, phase)
    return {
        "as_of_date": today,
        "cycle_phase": phase,
        "thesis": thesis,
        "chart_path": chart_path,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_long_thesis_refresh()
    print(json.dumps({k: v for k, v in result.items() if k != "thesis"}, indent=2))
    print(json.dumps(result.get("thesis"), indent=2))
