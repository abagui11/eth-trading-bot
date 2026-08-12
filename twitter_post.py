"""Twitter/X announcements for HQ trade cards and the daily digest.

Announcement-only: X has no Accept/Reject, so tweets mirror the Telegram
card copy. Posts use API v2 (POST /2/tweets) with OAuth 1.0a user context
on the pay-per-use plan. Everything is fail-soft — missing keys, missing
package, or API errors log and return None so Telegram delivery is never
blocked by X.
"""

from __future__ import annotations

import logging

import requests

import config
import display_summary
from models import Suggestion

logger = logging.getLogger(__name__)

_TWEETS_URL = "https://api.x.com/2/tweets"
TWEET_MAX_CHARS = 280


def is_enabled() -> bool:
    """True when TWITTER_ENABLED and all four OAuth 1.0a keys are set."""
    return bool(
        config.TWITTER_ENABLED
        and config.TWITTER_API_KEY
        and config.TWITTER_API_SECRET
        and config.TWITTER_ACCESS_TOKEN
        and config.TWITTER_ACCESS_TOKEN_SECRET
    )


def _oauth():
    from requests_oauthlib import OAuth1

    return OAuth1(
        config.TWITTER_API_KEY,
        client_secret=config.TWITTER_API_SECRET,
        resource_owner_key=config.TWITTER_ACCESS_TOKEN,
        resource_owner_secret=config.TWITTER_ACCESS_TOKEN_SECRET,
    )


def post_tweet(text: str, *, in_reply_to: str | None = None) -> str | None:
    """Post one tweet; return the tweet id, or None on any failure."""
    if not is_enabled():
        logger.info("Twitter posting disabled or keys missing — skipping tweet")
        return None
    body: dict = {"text": text.strip()[:TWEET_MAX_CHARS]}
    if in_reply_to:
        body["reply"] = {"in_reply_to_tweet_id": in_reply_to}
    try:
        res = requests.post(_TWEETS_URL, json=body, auth=_oauth(), timeout=20)
        if res.status_code >= 300:
            logger.error(
                "Tweet failed (%s): %s", res.status_code, res.text[:300]
            )
            return None
        tweet_id = str(res.json()["data"]["id"])
        logger.info("Tweet posted: %s", tweet_id)
        return tweet_id
    except Exception:
        logger.exception("Tweet post failed")
        return None


def post_thread(texts: list[str]) -> list[str]:
    """Post tweets as a reply chain; stops on first failure. Returns posted ids."""
    posted: list[str] = []
    reply_to: str | None = None
    for text in texts:
        tweet_id = post_tweet(text, in_reply_to=reply_to)
        if tweet_id is None:
            break
        posted.append(tweet_id)
        reply_to = tweet_id
    return posted


def format_hq_tweet(
    suggestion: Suggestion,
    *,
    summary: str | None = None,
) -> str | None:
    """Announcement copy for an HQ hourly trade card. None for no_trade/unsized."""
    if suggestion.action == "no_trade":
        return None
    if suggestion.entry is None or suggestion.stop_loss is None:
        return None

    title = f"High Quality · {display_summary.friendly_title(suggestion)}"
    lines = [title, ""]
    lines.append(f"Entry {float(suggestion.entry):,.2f}")
    lines.append(f"SL    {float(suggestion.stop_loss):,.2f}")
    for i, tp in enumerate((suggestion.take_profits or [])[:3], start=1):
        lines.append(f"TP{i}   {float(tp):,.2f}")
    base = "\n".join(lines)

    blurb = (summary or "").strip()
    if not blurb:
        try:
            blurb = display_summary.deterministic_setup_blurb(suggestion)
        except Exception:
            blurb = ""
    if blurb:
        room = TWEET_MAX_CHARS - len(base) - 2  # "\n\n" separator
        if room > 20:
            if len(blurb) > room:
                blurb = blurb[: room - 1].rstrip() + "…"
            base = f"{base}\n\n{blurb}"
    return base[:TWEET_MAX_CHARS]


def announce_hq(suggestion: Suggestion, *, summary: str | None = None) -> str | None:
    """Format + post an HQ trade announcement. Fail-soft; returns tweet id."""
    try:
        text = format_hq_tweet(suggestion, summary=summary)
    except Exception:
        logger.exception("HQ tweet formatting failed")
        return None
    if not text:
        return None
    return post_tweet(text)
