"""Metrics fetcher tests with mocked HTTP."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

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
    assert snap.source == "binance"


def test_fetch_funding_falls_back_to_bybit():
    bybit_ticker = {
        "result": {
            "list": [
                {
                    "symbol": "ETHUSDT",
                    "fundingRate": "0.00015",
                    "nextFundingTime": "123",
                }
            ]
        }
    }
    bybit_history = {
        "result": {
            "list": [
                {"fundingRate": "0.0002"},
                {"fundingRate": "0.0001"},
            ]
        }
    }

    def _side_effect(url, params=None, timeout=20.0):
        if "binance.com" in url:
            raise requests.HTTPError("451 blocked")
        if "tickers" in url:
            return bybit_ticker
        return bybit_history

    with patch("metrics.fetch._get_json", side_effect=_side_effect):
        snap = fetch.fetch_funding()

    assert snap.source == "bybit"
    assert snap.current_rate_pct == pytest.approx(0.015)


def test_fetch_spot_volume_from_h1_bars():
    bars = [
        {"volume": 100.0, "close": 2000.0},
        {"volume": 50.0, "close": 2100.0},
    ]
    with patch("research.get_ohlc", return_value=bars):
        snap = fetch.fetch_spot_volume()

    assert snap.volume_24h_base == 150.0
    assert snap.volume_24h_quote == 100 * 2000 + 50 * 2100
    assert snap.source == "coinbase_h1"


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


def test_miner_hashprice_from_blockchain_stats():
    btc_product = {"product": {"price": "60000"}}
    stats = {"hash_rate": 500_000.0}  # GH/s

    def _side_effect(url, params=None, timeout=20.0):
        if "hashrateindex" in url:
            raise requests.HTTPError("404")
        if "blockchain.info/stats" in url:
            return stats
        if "BTC-USD" in url:
            return btc_product
        raise RuntimeError(url)

    with patch("metrics.fetch._get_json", side_effect=_side_effect):
        snap = fetch.fetch_miner_breakeven()

    assert snap.hashprice_usd_per_ph_per_day is not None
    assert snap.estimated_breakeven_usd is not None
    assert snap.btc_spot_usd == 60000.0


def test_cache_prevents_duplicate_fetches():
    calls = {"n": 0}

    def _fetch() -> int:
        calls["n"] += 1
        return 42

    assert cache.get_or_fetch("test_key", _fetch, ttl_sec=60) == 42
    assert cache.get_or_fetch("test_key", _fetch, ttl_sec=60) == 42
    assert calls["n"] == 1
