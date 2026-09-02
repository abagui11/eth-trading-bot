"""Once-daily performance digest: "you'd be up X%" + winning-trade breakdown.

Built from the mill volume paper book (every sized idea sent to subscribers),
not Eva HQ paper and not the internal live mill clip. Rows opened before
``MILL_PAPER_EPOCH_START`` are ignored so the restart is a clean mill epoch.
Posted as an X thread and mirrored to Telegram subscribers as plain text.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import bot_config
import trade_ideas_bridge
import twitter_post

logger = logging.getLogger(__name__)

_MAX_WINNERS = 3
_RATIONALE_ONE_LINER_CHARS = 90
_CLOSED = frozenset({"hit_tp", "hit_sl"})


def _rationale_one_liner(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\[[^\]]*\]\s*", "", text)
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].strip()
    if len(first) > _RATIONALE_ONE_LINER_CHARS:
        first = first[: _RATIONALE_ONE_LINER_CHARS - 1].rstrip() + "…"
    return first


def _unrealized_pct(direction: str, entry: float, spot: float) -> float:
    if direction == "long":
        return (spot - entry) / entry * 100.0
    if direction == "short":
        return (entry - spot) / entry * 100.0
    return 0.0


def build_digest(spots: dict[str, float] | None = None) -> dict[str, Any]:
    """Aggregate mill volume-paper performance + recent TP winners."""
    since = str(bot_config.MILL_PAPER_EPOCH_START or "").strip()
    trades = trade_ideas_bridge.mill_paper_trades_since(since)
    closed = [t for t in trades if str(t.get("status") or "") in _CLOSED]
    opens = [t for t in trades if str(t.get("status") or "") == "open"]
    wins = [t for t in closed if str(t.get("status") or "") == "hit_tp"]
    losses = [t for t in closed if str(t.get("status") or "") == "hit_sl"]

    realized = sum(float(t.get("pnl_pct") or 0) for t in closed)
    unrealized = 0.0
    for trade in opens:
        product = str(trade.get("product_id") or "")
        entry = trade.get("entry")
        direction = str(trade.get("direction") or "")
        spot = (spots or {}).get(product)
        if entry is None or spot is None or direction not in ("long", "short"):
            continue
        unrealized += _unrealized_pct(direction, float(entry), float(spot))

    wins_sorted = sorted(
        wins,
        key=lambda t: str(t.get("closed_at") or ""),
        reverse=True,
    )
    winners: list[dict[str, Any]] = []
    for trade in wins_sorted[:_MAX_WINNERS]:
        product_id = str(trade.get("product_id") or "ETH-USD")
        winners.append(
            {
                "product_label": bot_config.product_label(product_id),
                "side": str(trade.get("direction") or ""),
                "pnl_pct": float(trade.get("pnl_pct") or 0.0),
                "rationale": _rationale_one_liner(
                    str(trade.get("blurb") or trade.get("title") or "")
                ),
            }
        )

    total = len(wins) + len(losses)
    total_pnl_pct = realized + unrealized
    return {
        "total_pnl_pct": round(total_pnl_pct, 2),
        "realized_pnl_pct": round(realized, 2),
        "unrealized_pnl_pct": round(unrealized, 2),
        "since": since,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / total * 100, 1) if total else 0.0,
        "winners": winners,
    }


def _headline(digest: dict[str, Any]) -> str:
    pct = float(digest["total_pnl_pct"])
    if pct >= 0:
        return (
            "If you've been following these trades you'd be up "
            f"+{pct:.1f}%."
        )
    return (
        "If you've been following these trades you'd be down "
        f"{abs(pct):.1f}%."
    )


def _winner_line(w: dict[str, Any]) -> str:
    line = f"• {w['product_label']} {w['side']} +{w['pnl_pct']:.1f}%"
    if w.get("rationale"):
        line += f" — {w['rationale']}"
    return line


def format_digest_text(digest: dict[str, Any]) -> str:
    """Full digest body for the Telegram mirror."""
    lines = [_headline(digest), ""]
    lines.append(
        f"Wins: {digest['wins']} · Losses: {digest['losses']} · "
        f"Win rate {digest['win_rate_pct']:.0f}%"
    )
    if digest["winners"]:
        lines.append("Recent winners:")
        for w in digest["winners"]:
            lines.append(_winner_line(w))
    return "\n".join(lines)[:4096]


def format_digest_tweets(digest: dict[str, Any]) -> list[str]:
    """Digest as a tweet thread: headline first, winner breakdown after."""
    limit = twitter_post.TWEET_MAX_CHARS
    first = (
        f"{_headline(digest)}\n\n"
        f"Wins: {digest['wins']} · Losses: {digest['losses']} · "
        f"Win rate {digest['win_rate_pct']:.0f}%"
    )
    tweets = [first[:limit]]

    if digest["winners"]:
        body = "Recent winners:"
        for w in digest["winners"]:
            line = _winner_line(w)
            candidate = f"{body}\n{line}"
            if len(candidate) > limit:
                tweets.append(body[:limit])
                body = line
            else:
                body = candidate
        tweets.append(body[:limit])
    return tweets


def run_daily_digest() -> None:
    """Build the digest, post the X thread, mirror to Telegram subscribers."""
    if not bot_config.DAILY_PERFORMANCE_POST_ENABLED:
        return
    try:
        import research

        spots = research.get_spot_prices()
    except Exception:
        logger.exception("Spot fetch failed for daily digest — marking closed only")
        spots = None

    digest = build_digest(spots)
    if digest["wins"] + digest["losses"] == 0:
        logger.info("Daily digest skipped — no closed mill trades in this epoch")
        return

    tweet_ids = twitter_post.post_thread(format_digest_tweets(digest))
    logger.info("Daily digest posted %s tweet(s)", len(tweet_ids))

    try:
        import notify

        notify.broadcast_plain_text(format_digest_text(digest))
    except Exception:
        logger.exception("Daily digest Telegram mirror failed")
