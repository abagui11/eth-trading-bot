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


def _enable_twitter(monkeypatch) -> None:
    monkeypatch.setattr(config, "TWITTER_ENABLED", True)
    monkeypatch.setattr(config, "TWITTER_API_KEY", "k")
    monkeypatch.setattr(config, "TWITTER_API_SECRET", "s")
    monkeypatch.setattr(config, "TWITTER_ACCESS_TOKEN", "t")
    monkeypatch.setattr(config, "TWITTER_ACCESS_TOKEN_SECRET", "ts")
    monkeypatch.setattr(twitter_post, "_oauth", lambda: None)


class _Resp:
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text or str(payload or "")

    def json(self) -> dict:
        return self._payload


def test_announce_hq_attaches_decision_chart(monkeypatch, tmp_path):
    _enable_twitter(monkeypatch)
    structure = tmp_path / "cycle_H4_structure.png"
    decision = tmp_path / "cycle_M5_decision.png"
    structure.write_bytes(b"struct")
    decision.write_bytes(b"\x89PNG\r\n\x1a\ndecision")
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url == twitter_post._TWEETS_URL:
            return _Resp(201, {"data": {"id": "hq1"}})
        params = kwargs.get("params") or {}
        if params.get("command") == "INIT":
            return _Resp(200, {"data": {"id": "media-dec"}})
        if params.get("command") == "APPEND":
            return _Resp(204)
        if params.get("command") == "FINALIZE":
            return _Resp(200, {"data": {"id": "media-dec"}})
        return _Resp(500, text="unexpected")

    monkeypatch.setattr(twitter_post.requests, "post", fake_post)
    tweet_id = twitter_post.announce_hq(
        _suggestion(),
        summary="Bullish structure holds.",
        chart_paths=[str(structure), str(decision)],
    )
    assert tweet_id == "hq1"
    tweet_call = next(c for c in calls if c["url"] == twitter_post._TWEETS_URL)
    assert tweet_call["json"]["media"]["media_ids"] == ["media-dec"]
    append = next(
        c
        for c in calls
        if (c.get("params") or {}).get("command") == "APPEND"
    )
    assert "decision" in append["files"]["media"][0]


def test_announce_hq_text_only_when_upload_fails(monkeypatch, tmp_path):
    _enable_twitter(monkeypatch)
    chart = tmp_path / "cycle_decision.png"
    chart.write_bytes(b"png")
    bodies: list[dict] = []

    def fake_post(url, **kwargs):
        if url == twitter_post._TWEETS_URL:
            bodies.append(kwargs.get("json") or {})
            return _Resp(201, {"data": {"id": "hq2"}})
        return _Resp(403, text="forbidden")

    monkeypatch.setattr(twitter_post.requests, "post", fake_post)
    assert twitter_post.announce_hq(_suggestion(), chart_paths=[str(chart)]) == "hq2"
    assert "media" not in bodies[0]


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


def test_build_digest_uses_mill_epoch_not_eva_paper(monkeypatch):
    rows = [
        {
            "status": "hit_sl",
            "product_id": "ETH-USD",
            "direction": "short",
            "pnl_pct": -1.15,
            "opened_at": "2026-09-02T12:00:00Z",
            "closed_at": "2026-09-02T13:44:47Z",
            "blurb": "New York open: ETH session setup",
            "entry": 2400.0,
        },
        {
            "status": "hit_tp",
            "product_id": "ETH-USD",
            "direction": "short",
            "pnl_pct": 1.52,
            "opened_at": "2026-09-02T09:00:00Z",
            "closed_at": "2026-09-02T10:33:57Z",
            "blurb": "BTC flipped bearish — ETH follow-on.",
            "entry": 2410.0,
        },
        {
            "status": "hit_tp",
            "product_id": "BTC-USD",
            "direction": "short",
            "pnl_pct": 1.31,
            "opened_at": "2026-09-01T08:00:00Z",
            "closed_at": "2026-09-02T09:37:30Z",
            "blurb": "London open: BTC session setup",
            "entry": 77000.0,
        },
        {
            "status": "open",
            "product_id": "ETH-USD",
            "direction": "short",
            "pnl_pct": None,
            "opened_at": "2026-09-02T14:00:00Z",
            "closed_at": None,
            "blurb": "still open",
            "entry": 2400.0,
        },
    ]
    monkeypatch.setattr(performance_digest.bot_config, "MILL_PAPER_EPOCH_START", "2026-09-01")
    monkeypatch.setattr(
        performance_digest.trade_ideas_bridge, "mill_paper_trades_since", lambda since: rows
    )
    digest = performance_digest.build_digest(spots={"ETH-USD": 2376.0})
    assert digest["wins"] == 2
    assert digest["losses"] == 1
    assert digest["win_rate_pct"] == 66.7
    # closed +1.52+1.31-1.15 = +1.68; open short 2400→2376 = +1.0
    assert abs(digest["total_pnl_pct"] - 2.68) < 0.02
    assert digest["winners"][0]["pnl_pct"] == 1.52
    assert "ETH follow-on" in digest["winners"][0]["rationale"]
    text = performance_digest.format_digest_text(digest)
    assert "ETH short +1.5%" in text
    assert "Asset preference" not in text
