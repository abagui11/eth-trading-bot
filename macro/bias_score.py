"""Deterministic + LLM-refined news bias scoring for the intelligence hub.

Pipeline:
1. On ingest/classify — assign a deterministic bias side + pct from keywords
   and categorical eth_bias. Dashboard shows this immediately.
2. On the hourly stance cycle — one batched Claude call refines scores for
   active events; results overwrite bias_pct_llm / bias_side_llm.
3. Trade ideas mill reads the best available score (LLM if present, else det).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

import bot_config
import config
from analyze import log_anthropic_usage
from macro import keywords as kw
from macro import store

logger = logging.getLogger(__name__)

# Phrases that lean bullish / bearish for crypto risk assets.
_BULLISH_HINTS = (
    "rate cut", "dovish", "etf approval", "etf inflow", "signed into law",
    "clarity act", "genius act", "ceasefire", "risk on", "spot inflow",
    "institutional", "passes the", "clears the",
)
_BEARISH_HINTS = (
    "rate hike", "hawkish", "etf outflow", "etf rejection", "depeg",
    "exchange hack", "exploit", "lawsuit", "sanctions", "ban", "war",
    "hormuz", "liquidation", "insolvency", "halt", "risk off",
)


def deterministic_bias(title: str, eth_bias: str | None = None) -> dict[str, Any]:
    """Return {side, pct, source='deterministic'} from headline text + optional LLM bias."""
    text = (title or "").lower()
    bull = sum(1 for p in _BULLISH_HINTS if p in text)
    bear = sum(1 for p in _BEARISH_HINTS if p in text)

    # Keyword tier strength as a base conviction (0-100 relevance is separate).
    relevance, _hits = kw.relevance_score(title or "")
    relevance = int(relevance or 0)

    categorical = (eth_bias or "neutral").lower()
    if categorical == "bullish":
        side = "bullish"
        base = 55
    elif categorical == "bearish":
        side = "bearish"
        base = 55
    elif categorical == "mixed":
        side = "bullish" if bull >= bear else ("bearish" if bear > bull else "neutral")
        base = 45
    else:
        if bull > bear:
            side = "bullish"
        elif bear > bull:
            side = "bearish"
        else:
            side = "neutral"
        base = 35

    # Nudge with phrase counts and relevance.
    nudge = min(25, abs(bull - bear) * 8) + min(15, relevance // 5)
    if side == "neutral":
        pct = min(40, 20 + relevance // 10)
    else:
        pct = min(92, max(30, base + nudge))
        if categorical in ("bullish", "bearish") and categorical != side:
            # Prefer categorical when present.
            side = categorical
            pct = min(92, max(pct, 50))

    return {"side": side, "pct": int(pct), "source": "deterministic"}


_BATCH_SYSTEM = """You refine crypto-macro headline bias for an ETH/BTC intelligence desk.
For each headline return a calibrated directional score.

Return JSON only:
{"items":[{"id":123,"side":"bullish|bearish|neutral","pct":0-100,"one_liner":"≤140 chars"}]}

Rules:
- pct is conviction that the named side is correct for near-term ETH/BTC risk appetite.
- neutral only when truly balanced; still give a pct for how muted the impact is (low).
- Be nuanced: sell-the-news, already-priced, and second-order effects matter.
- Do not invent facts not in the headline.
- Keep the same id for each input item. One entry per input id.
"""


def refine_bias_batch(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One Claude call for many events. Returns [{id, side, pct, one_liner}, ...]."""
    if not events:
        return []
    payload = []
    for e in events:
        payload.append(
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "summary": (e.get("summary") or e.get("eth_impact_summary") or "")[:280],
                "severity": e.get("severity"),
                "current_bias": e.get("eth_bias"),
                "det_side": e.get("bias_side_det"),
                "det_pct": e.get("bias_pct_det"),
            }
        )
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL_FAST,
            max_tokens=2048,
            system=_BATCH_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": "Headlines JSON:\n"
                    + json.dumps(payload)
                    + "\nReturn JSON only.",
                }
            ],
        )
    except Exception:
        logger.exception("Batched bias refine failed")
        return []

    log_anthropic_usage(response, "macro_bias_batch")
    raw = ""
    for block in response.content:
        if block.type == "text":
            raw += block.text
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("Bias batch JSON parse failed")
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        side = str(item.get("side") or "neutral").lower()
        if side not in ("bullish", "bearish", "neutral"):
            side = "neutral"
        try:
            pct = int(item.get("pct") or 0)
        except (TypeError, ValueError):
            pct = 0
        pct = max(0, min(100, pct))
        out.append(
            {
                "id": int(item["id"]),
                "side": side,
                "pct": pct,
                "one_liner": str(item.get("one_liner") or "")[:200],
            }
        )
    return out


def run_hourly_bias_refine(*, limit: int = 40) -> int:
    """Refine active/recent events in one batch. Returns number updated."""
    if not bot_config.MACRO_CONTEXT_ENABLED:
        return 0
    active = store.get_active_events(min_severity=1)
    recent = store.list_events(limit=limit)
    by_id: dict[int, dict[str, Any]] = {}
    for e in active + recent:
        eid = e.get("id")
        if eid is None:
            continue
        by_id[int(eid)] = e
    events = list(by_id.values())[:limit]
    # Prefer events that still lack an LLM score.
    events.sort(key=lambda e: (0 if e.get("bias_pct_llm") is None else 1, -(e.get("id") or 0)))
    refined = refine_bias_batch(events)
    updated = 0
    for item in refined:
        store.update_bias_llm(
            item["id"],
            side=item["side"],
            pct=item["pct"],
            one_liner=item.get("one_liner"),
        )
        updated += 1
    return updated


def best_bias(event: dict[str, Any]) -> dict[str, Any]:
    """Prefer LLM score when present."""
    if event.get("bias_pct_llm") is not None and event.get("bias_side_llm"):
        return {
            "side": event["bias_side_llm"],
            "pct": int(event["bias_pct_llm"]),
            "source": "llm",
            "one_liner": event.get("bias_one_liner") or event.get("eth_impact_summary"),
        }
    if event.get("bias_pct_det") is not None and event.get("bias_side_det"):
        return {
            "side": event["bias_side_det"],
            "pct": int(event["bias_pct_det"]),
            "source": "deterministic",
            "one_liner": event.get("eth_impact_summary"),
        }
    det = deterministic_bias(str(event.get("title") or ""), event.get("eth_bias"))
    return {
        "side": det["side"],
        "pct": det["pct"],
        "source": "deterministic",
        "one_liner": event.get("eth_impact_summary"),
    }
