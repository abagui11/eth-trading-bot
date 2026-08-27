"""Twitter/X announcements for HQ trade cards and the daily digest.

Announcement-only: X has no Accept/Reject, so tweets mirror the Telegram
card copy and attach the decision chart when one was rendered. Posts use
API v2 (POST /2/tweets) with OAuth 1.0a user context on the pay-per-use
plan. Media uses chunked upload on /2/media/upload, with a v1.1
simple-upload fallback. Everything is fail-soft — missing keys, missing
package, or API errors log and return None so Telegram delivery is never
blocked by X.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

import config
import display_summary
from models import Suggestion

logger = logging.getLogger(__name__)

_TWEETS_URL = "https://api.x.com/2/tweets"
_MEDIA_V2_URL = "https://api.x.com/2/media/upload"
_MEDIA_V1_URL = "https://upload.twitter.com/1.1/media/upload.json"
TWEET_MAX_CHARS = 280
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


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


def _media_id_from(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    raw = (
        (data or {}).get("id")
        or payload.get("media_id_string")
        or payload.get("media_id")
    )
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _upload_v2(path: Path, content_type: str, size: int) -> str | None:
    auth = _oauth()
    init = requests.post(
        _MEDIA_V2_URL,
        params={
            "command": "INIT",
            "total_bytes": size,
            "media_type": content_type,
            "media_category": "tweet_image",
        },
        auth=auth,
        timeout=30,
    )
    if init.status_code >= 300:
        logger.error("Tweet media INIT failed (%s): %s", init.status_code, init.text[:300])
        return None
    media_id = _media_id_from(init.json())
    if not media_id:
        logger.error("Tweet media INIT missing id")
        return None

    with path.open("rb") as fh:
        append = requests.post(
            _MEDIA_V2_URL,
            params={
                "command": "APPEND",
                "media_id": media_id,
                "segment_index": 0,
            },
            files={"media": (path.name, fh, content_type)},
            auth=auth,
            timeout=60,
        )
    if append.status_code >= 300:
        logger.error(
            "Tweet media APPEND failed (%s): %s", append.status_code, append.text[:300]
        )
        return None

    finalize = requests.post(
        _MEDIA_V2_URL,
        params={"command": "FINALIZE", "media_id": media_id},
        auth=auth,
        timeout=30,
    )
    if finalize.status_code >= 300:
        logger.error(
            "Tweet media FINALIZE failed (%s): %s",
            finalize.status_code,
            finalize.text[:300],
        )
        return None
    return _media_id_from(finalize.json()) or media_id


def _upload_v1(path: Path, content_type: str) -> str | None:
    with path.open("rb") as fh:
        res = requests.post(
            _MEDIA_V1_URL,
            files={"media": (path.name, fh, content_type)},
            auth=_oauth(),
            timeout=60,
        )
    if res.status_code >= 300:
        logger.error(
            "Tweet media v1 upload failed (%s): %s", res.status_code, res.text[:300]
        )
        return None
    return _media_id_from(res.json())


def upload_media(path: str | Path) -> str | None:
    """Upload a local image; return media id, or None on any failure."""
    if not is_enabled():
        return None
    p = Path(path)
    if not p.is_file():
        logger.warning("Tweet image missing: %s", p)
        return None
    content_type = _IMAGE_TYPES.get(p.suffix.lower())
    if not content_type:
        logger.warning("Tweet image type not supported: %s", p)
        return None
    size = p.stat().st_size
    if size <= 0 or size > _MAX_IMAGE_BYTES:
        logger.warning("Tweet image skipped (size %s): %s", size, p)
        return None
    try:
        media_id = _upload_v2(p, content_type, size)
        if media_id:
            logger.info("Tweet media uploaded: %s", media_id)
            return media_id
        media_id = _upload_v1(p, content_type)
        if media_id:
            logger.info("Tweet media uploaded (v1): %s", media_id)
        return media_id
    except Exception:
        logger.exception("Tweet media upload failed")
        return None


def _decision_chart_paths(chart_paths: list[str] | str | None) -> list[str]:
    if not chart_paths:
        return []
    paths = [chart_paths] if isinstance(chart_paths, str) else list(chart_paths)
    paths = [p for p in paths if p and p != "watchdog"]
    decision = [p for p in paths if "decision" in str(p).lower()]
    return (decision or paths)[:1]


def post_tweet(
    text: str,
    *,
    in_reply_to: str | None = None,
    media_ids: list[str] | None = None,
) -> str | None:
    """Post one tweet; return the tweet id, or None on any failure."""
    if not is_enabled():
        logger.info("Twitter posting disabled or keys missing — skipping tweet")
        return None
    body: dict[str, Any] = {"text": text.strip()[:TWEET_MAX_CHARS]}
    if in_reply_to:
        body["reply"] = {"in_reply_to_tweet_id": in_reply_to}
    ids = [m for m in (media_ids or []) if m][:4]
    if ids:
        body["media"] = {"media_ids": ids}
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


def announce_hq(
    suggestion: Suggestion,
    *,
    summary: str | None = None,
    chart_paths: list[str] | str | None = None,
) -> str | None:
    """Format + post an HQ trade announcement with its decision chart. Fail-soft."""
    try:
        text = format_hq_tweet(suggestion, summary=summary)
    except Exception:
        logger.exception("HQ tweet formatting failed")
        return None
    if not text:
        return None
    media_ids: list[str] = []
    for path in _decision_chart_paths(chart_paths):
        media_id = upload_media(path)
        if media_id:
            media_ids.append(media_id)
    return post_tweet(text, media_ids=media_ids or None)
