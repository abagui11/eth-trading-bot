"""Tests: High Quality card label, HQ tweet formatting, daily digest copy."""

from __future__ import annotations

import config
import display_summary
import performance_digest
import twitter_post
from models import Suggestion


def _suggestion(rationale: str = "Clean bullish structure holds.") -> Suggestion:
    return Suggestion(
        action="spot_buy",
        size=100.0,
        entry=2000.0,
        stop_loss=1900.0,
        take_profits=[2200.0, 2400.0],
        risk_reward=2.0,
        rationale=rationale,
        product_id="ETH-USD",
    )


def test_card_body_has_high_quality_label():
    body = display_summary.build_card_body(_suggestion())
    assert body.startswith("High Quality · ETH Spot Buy")


def test_card_body_watchdog_has_no_label():
    suggestion = _suggestion(rationale="[Watchdog — m5_ob_fib_long]\n\nSetup.")
    body = display_summary.build_card_body(suggestion)
    assert body.startswith("ETH Spot Buy")
    assert "High Quality" not in body


def test_format_hq_tweet_label_levels_and_length():
    text = twitter_post.format_hq_tweet(
        _suggestion(), summary="Bullish structure aligns with M5 fib. " * 20
    )
    assert text is not None
    assert text.startswith("High Quality · ETH Spot Buy")
    assert "Entry 2,000.00" in text
    assert "SL    1,900.00" in text
    assert "TP1   2,200.00" in text
    assert len(text) <= twitter_post.TWEET_MAX_CHARS


def test_format_hq_tweet_skips_no_trade():
    suggestion = Suggestion(
        action="no_trade",
        size=0.0,
        entry=None,
        stop_loss=None,
        rationale="Nothing lines up.",
        product_id="ETH-USD",
    )
    assert twitter_post.format_hq_tweet(suggestion) is None


def test_post_tweet_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "TWITTER_ENABLED", False)
    assert twitter_post.post_tweet("hello") is None


def _digest() -> dict:
    return {
        "total_pnl_pct": 12.4,
        "equity_usd": 5620.0,
        "starting_usd": 5000.0,
        "wins": 8,
        "losses": 3,
        "win_rate_pct": 72.7,
        "winners": [
            {
                "product_label": "ETH",
                "side": "long",
                "pnl_pct": 4.1,
                "pnl_usd": 205.0,
                "rationale": "H4 order block held after the sweep.",
            },
            {
                "product_label": "BTC",
                "side": "short",
                "pnl_pct": 2.0,
                "pnl_usd": 100.0,
                "rationale": "Bearish displacement off HTF resistance.",
            },
        ],
    }


def test_digest_tweets_headline_and_length():
    tweets = performance_digest.format_digest_tweets(_digest())
    assert tweets, "expected at least one tweet"
    assert "you'd be up +12.4%" in tweets[0]
    assert "Win rate 73%" in tweets[0]
    for tweet in tweets:
        assert len(tweet) <= twitter_post.TWEET_MAX_CHARS
    joined = "\n".join(tweets)
    assert "ETH long +4.1%" in joined
    assert "BTC short +2.0%" in joined


def test_digest_headline_negative():
    digest = _digest() | {"total_pnl_pct": -3.2}
    text = performance_digest.format_digest_text(digest)
    assert "you'd be down 3.2%" in text


def test_digest_text_breakdown():
    text = performance_digest.format_digest_text(_digest())
    assert "Wins: 8 · Losses: 3" in text
    assert "H4 order block held after the sweep." in text
