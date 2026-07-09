"""Metrics fetcher tests with mocked HTTP."""

from __future__ import annotations

from unittest.mock import patch

from metrics import cache, fetch


def setup_function() -> None:
    cache.clear_cache()


def test_fetch_funding_parses_binance_response():
    premium = {"lastFundingRate": "0.0001", "nextFundingTime": 1234567890000}
    history = [{"fundingRate": "0.0002"}, {"fundingRate": "0.0001"}]

    with patch("metrics.fetch._get_json") as mock_get:
        mock_get.side_effect = [premium, history]
        snap = fetch.fetch_funding()

    assert snap.current_rate_pct == 0.01
    assert snap.avg_7d_pct is not None


def test_fetch_dominance_parses_coingecko():
    global_data = {
        "data": {
            "market_cap_percentage": {"btc": 52.5},
            "total_market_cap": {"usd": 3_000_000_000_000},
        }
    }
    tether = {"market_data": {"market_cap": {"usd": 120_000_000_000}}}

    with patch("metrics.fetch._get_json") as mock_get:
        mock_get.side_effect = [global_data, tether]
        snap = fetch.fetch_dominance()

    assert snap.btc_dominance_pct == 52.5
    assert snap.usdt_dominance_pct is not None
    assert snap.usdt_dominance_pct > 0


def test_cache_prevents_duplicate_fetches():
    calls = {"n": 0}

    def _fetch() -> int:
        calls["n"] += 1
        return 42

    assert cache.get_or_fetch("test_key", _fetch, ttl_sec=60) == 42
    assert cache.get_or_fetch("test_key", _fetch, ttl_sec=60) == 42
    assert calls["n"] == 1
