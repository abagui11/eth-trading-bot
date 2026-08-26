"""Once-daily performance digest: "you'd be up X%" + winning-trade breakdown.

Built from the house paper book (true equity vs starting capital) via
dashboard.performance.build_performance, with winner rationales pulled from
the ledger by open cycle id. Posted as an X thread and mirrored to Telegram
subscribers as plain text.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import bot_config
import ledger
import paper
import twitter_post
from dashboard.performance import build_performance

logger = logging.getLogger(__name__)

_MAX_WINNERS = 3
_RATIONALE_ONE_LINER_CHARS = 90


def _rationale_one_liner(open_cycle_id: str | None) -> str:
    """First sentence of the ledger thesis for a cycle, tightly truncated."""
    if not open_cycle_id:
        return ""
    try:
        row = ledger.get_suggestion_by_cycle_id(str(open_cycle_id))
    except Exception:
        logger.exception("Ledger lookup failed for cycle %s", open_cycle_id)
        return ""
    raw = str((row or {}).get("rationale") or "").strip()
    if not raw:
        return ""
    # Drop watchdog/source prefixes like "[Watchdog — m5_ob_fib_long]".
    raw = re.sub(r"^\[[^\]]*\]\s*", "", raw)
    first = re.split(r"(?<=[.!?])\s", raw, maxsplit=1)[0].strip()
    if len(first) > _RATIONALE_ONE_LINER_CHARS:
        first = first[: _RATIONALE_ONE_LINER_CHARS - 1].rstrip() + "…"
    return first


def build_digest(spots: dict[str, float] | None = None) -> dict[str, Any]:
    """Aggregate house-book performance + recent winners with rationale."""
    perf = build_performance(spots=spots)
    # Same scope as the headline P&L (whole epoch) — a windowed win rate next
    # to all-time P&L reads as a contradiction when the window differs.
    closed = paper.get_closed_trades(limit=500)

    wins = [t for t in closed if float(t.get("realized_pnl_usd") or 0) > 0]
    losses = [t for t in closed if float(t.get("realized_pnl_usd") or 0) <= 0]

    winners: list[dict[str, Any]] = []
    for trade in wins[:_MAX_WINNERS]:
        product_id = str(trade.get("product_id") or "ETH-USD")
        winners.append(
            {
                "product_label": bot_config.product_label(product_id),
                "side": str(trade.get("side") or ""),
                "pnl_pct": float(trade.get("realized_pnl_pct") or 0.0),
                "pnl_usd": float(trade.get("realized_pnl_usd") or 0.0),
                "rationale": _rationale_one_liner(trade.get("open_cycle_id")),
            }
        )

    total = len(wins) + len(losses)
    return {
        "total_pnl_pct": float(perf.get("total_pnl_pct") or 0.0),
        "equity_usd": float(perf.get("equity_usd") or 0.0),
        "starting_usd": float(perf.get("starting_usd") or 0.0),
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
        logger.exception("Spot fetch failed for daily digest — marking at entry")
        spots = None

    digest = build_digest(spots)
    if digest["wins"] + digest["losses"] == 0:
        logger.info("Daily digest skipped — no closed trades yet")
        return

    tweet_ids = twitter_post.post_thread(format_digest_tweets(digest))
    logger.info("Daily digest posted %s tweet(s)", len(tweet_ids))

    try:
        import notify

        notify.broadcast_plain_text(format_digest_text(digest))
    except Exception:
        logger.exception("Daily digest Telegram mirror failed")
