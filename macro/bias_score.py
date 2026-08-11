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


_ONE_LINER_CHARS = 110

_BATCH_SYSTEM = f"""You refine crypto-macro headline bias for an ETH/BTC intelligence desk.
For each headline return a calibrated directional score.

Return JSON only — no markdown fence, no commentary. Emit it compactly, one
item object per line and no indentation:
{{"items":[{{"id":123,"side":"bullish|bearish|neutral","pct":0-100,"one_liner":"..."}}]}}

Rules:
- pct is conviction that the named side is correct for near-term ETH/BTC risk appetite.
- neutral only when truly balanced; still give a pct for how muted the impact is (low).
- Be nuanced: sell-the-news, already-priced, and second-order effects matter.
- Do not invent facts not in the headline.
- Keep the same id for each input item. One entry per input id.
- one_liner must be at most {_ONE_LINER_CHARS} characters.
"""

# Headlines are scored in bounded chunks rather than one unbounded call, so the
# batch can grow with news volume without any single reply outgrowing its token
# budget. This stays a batch job: one call covers many headlines.
_CHUNK_SIZE = 20
# A fully expanded reply (every JSON field on its own line) costs about 50
# tokens per item; budget well above that so the model's formatting choice can
# never truncate the reply.
_TOKENS_PER_ITEM = 90
_CHUNK_TOKEN_OVERHEAD = 256
_MAX_CHUNK_TOKENS = 8192


def _chunk_max_tokens(count: int) -> int:
    return min(_MAX_CHUNK_TOKENS, count * _TOKENS_PER_ITEM + _CHUNK_TOKEN_OVERHEAD)


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _snippet(raw: str, *, head: int = 400, tail: int = 200) -> str:
    """Head and tail of a reply, enough to see where and how it broke."""
    if len(raw) <= head + tail:
        return raw
    return f"{raw[:head]} …[{len(raw) - head - tail} chars omitted]… {raw[-tail:]}"


def _salvage_items(raw: str) -> list[dict[str, Any]]:
    """Recover every complete item object from a reply that will not parse.

    A reply cut off at max_tokens ends mid-object, which makes the whole
    document invalid even though every earlier entry arrived intact. Decoding
    object by object costs only the severed tail instead of the whole batch.
    """
    decoder = json.JSONDecoder()
    items: list[dict[str, Any]] = []
    idx = raw.find("{")
    while idx != -1:
        try:
            obj, end = decoder.raw_decode(raw, idx)
        except ValueError:
            idx = raw.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            # A complete wrapper means the reply was valid after all.
            if isinstance(obj.get("items"), list):
                return [i for i in obj["items"] if isinstance(i, dict)]
            if "id" in obj:
                items.append(obj)
        idx = raw.find("{", max(end, idx + 1))
    return items


def _parse_items(raw: str) -> tuple[list[dict[str, Any]], bool]:
    """(items, salvaged) — salvaged is True when the reply was not valid JSON."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _salvage_items(raw), True
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return _salvage_items(raw), True
    return [i for i in items if isinstance(i, dict)], False


def _normalize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        try:
            event_id = int(item["id"])
        except (TypeError, ValueError):
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
                "id": event_id,
                "side": side,
                "pct": pct,
                "one_liner": str(item.get("one_liner") or "")[:200],
            }
        )
    return out


def _chunk_payload(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": e.get("id"),
            "title": e.get("title"),
            "summary": (e.get("summary") or e.get("eth_impact_summary") or "")[:280],
            "severity": e.get("severity"),
            "current_bias": e.get("eth_bias"),
            "det_side": e.get("bias_side_det"),
            "det_pct": e.get("bias_pct_det"),
        }
        for e in events
    ]


def _refine_chunk(client: Any, chunk: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score one bounded chunk. Never raises; returns whatever it could parse."""
    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL_FAST,
            max_tokens=_chunk_max_tokens(len(chunk)),
            system=_BATCH_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": "Headlines JSON:\n"
                    + json.dumps(_chunk_payload(chunk))
                    + "\nReturn JSON only.",
                }
            ],
        )
    except Exception:
        logger.exception("Batched bias refine call failed for %s headlines", len(chunk))
        return []

    log_anthropic_usage(response, "macro_bias_batch")
    raw = _strip_fence(
        "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    )
    items, salvaged = _parse_items(raw)
    out = _normalize_items(items)
    stop_reason = getattr(response, "stop_reason", None)
    if salvaged or stop_reason == "max_tokens" or len(out) < len(chunk):
        usage = getattr(response, "usage", None)
        logger.error(
            "Bias batch reply incomplete: stop_reason=%s output_tokens=%s "
            "max_tokens=%s sent=%s recovered=%s salvaged=%s raw=%r",
            stop_reason,
            getattr(usage, "output_tokens", None),
            _chunk_max_tokens(len(chunk)),
            len(chunk),
            len(out),
            salvaged,
            _snippet(raw),
        )
    return out


def refine_bias_batch(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score many events in a few batched Claude calls.

    Returns [{id, side, pct, one_liner}, ...] for every entry that came back
    intact; a chunk that fails or arrives truncated costs only its own entries.
    """
    if not events:
        return []
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    out: list[dict[str, Any]] = []
    for start in range(0, len(events), _CHUNK_SIZE):
        out.extend(_refine_chunk(client, events[start : start + _CHUNK_SIZE]))
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
    if updated < len(events):
        logger.warning(
            "Bias refine covered %s of %s headlines — the rest stay deterministic",
            updated,
            len(events),
        )
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
