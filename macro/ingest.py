"""RSS polling and headline ingest pipeline."""

from __future__ import annotations

import logging
from typing import Any

import feedparser

import bot_config
import config
from macro import classify, pulse, store
from macro.keywords import relevance_score

logger = logging.getLogger(__name__)


def ingest_headline(
    *,
    title: str,
    url: str | None = None,
    summary: str | None = None,
    source: str | None = None,
    published_at: str | None = None,
    force_classify: bool = False,
) -> dict[str, Any] | None:
    """Score, optionally classify, store, and pulse on a single headline."""
    if not bot_config.MACRO_CONTEXT_ENABLED:
        return None

    title = title.strip()
    if not title:
        return None

    hash_value = store.url_hash(url, title)
    if not force_classify and store.has_recent_url_hash(hash_value):
        logger.debug("Macro ingest skipped duplicate: %s", title[:80])
        return None

    text = f"{title} {summary or ''}"
    score, hits = relevance_score(text, extra_t2=config.MACRO_KEYWORD_EXTRA)
    promote = force_classify or score >= bot_config.MACRO_LLM_PROMOTE_THRESHOLD

    if not promote:
        return store.insert_event(
            source=source,
            title=title,
            url=url,
            summary=summary,
            published_at=published_at,
            keyword_score=score,
            keyword_hits=hits,
            severity=0,
            status="ignored",
        )

    classification = classify.classify_headline(
        title=title,
        summary=summary,
        source=source,
    )
    expires_at = classify.expires_at_from_ttl(int(classification["ttl_hours"]))

    event = store.insert_event(
        source=source,
        title=title,
        url=url,
        summary=summary,
        published_at=published_at,
        keyword_score=score,
        keyword_hits=hits,
        severity=int(classification["severity"]),
        eth_bias=classification["eth_bias"],
        category=classification["category"],
        eth_impact_summary=classification["eth_impact_summary"],
        posture_hints=classification["posture_hints"],
        expires_at=expires_at,
        status="classified",
        raw_json=classification,
    )

    if int(event.get("severity") or 0) >= bot_config.MACRO_PULSE_MIN_SEVERITY:
        try:
            pulse.run_macro_pulse(event)
        except Exception:
            logger.exception("Macro pulse failed for event %s", event.get("id"))

    logger.info(
        "Macro classified: sev=%s bias=%s score=%s title=%s",
        event.get("severity"),
        event.get("eth_bias"),
        score,
        title[:80],
    )
    return event


def poll_feeds() -> int:
    """Poll configured RSS feeds; return count of newly ingested items."""
    if not bot_config.MACRO_CONTEXT_ENABLED:
        return 0
    if not config.MACRO_FEED_URLS:
        logger.debug("No MACRO_FEED_URLS configured — skipping poll")
        return 0

    ingested = 0
    for feed_url in config.MACRO_FEED_URLS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception:
            logger.exception("Failed to parse RSS feed %s", feed_url)
            continue

        source = parsed.feed.get("title") or feed_url
        for entry in parsed.entries[:25]:
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            link = str(entry.get("link") or "").strip() or None
            summary = str(entry.get("summary") or entry.get("description") or "").strip()
            if summary and len(summary) > 500:
                summary = summary[:500] + "..."
            published = entry.get("published") or entry.get("updated")
            published_at = str(published) if published else None

            result = ingest_headline(
                title=title,
                url=link,
                summary=summary or None,
                source=source,
                published_at=published_at,
            )
            if result is not None:
                ingested += 1

    try:
        store.prune_old_events()
    except Exception:
        logger.exception("Macro prune failed")

    if ingested:
        logger.info("Macro feed poll ingested %s new item(s)", ingested)
    return ingested
