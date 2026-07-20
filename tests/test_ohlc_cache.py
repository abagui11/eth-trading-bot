"""OHLC cache multi-product coverage tests."""

from __future__ import annotations

from pathlib import Path

import ohlc_cache


def test_cache_coverage_and_get_daily_respect_product_id(tmp_path: Path, monkeypatch):
    db = tmp_path / "ohlc.db"
    monkeypatch.setattr("config.OHLC_DB", db)
    monkeypatch.setattr(ohlc_cache.config, "OHLC_DB", db)

    eth_bars = [
        {
            "ts": "2024-01-01T00:00:00Z",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 10.0,
        },
        {
            "ts": "2024-01-02T00:00:00Z",
            "open": 1.5,
            "high": 2.5,
            "low": 1.0,
            "close": 2.0,
            "volume": 12.0,
        },
    ]
    btc_bars = [
        {
            "ts": "2024-01-01T00:00:00Z",
            "open": 40_000.0,
            "high": 41_000.0,
            "low": 39_000.0,
            "close": 40_500.0,
            "volume": 100.0,
        },
    ]

    ohlc_cache.upsert_candles(ohlc_cache.DAILY_GRANULARITY, eth_bars, product_id="ETH-USD")
    ohlc_cache.upsert_candles(ohlc_cache.DAILY_GRANULARITY, btc_bars, product_id="BTC-USD")

    eth_min, eth_max, eth_cnt = ohlc_cache.cache_coverage(
        ohlc_cache.DAILY_GRANULARITY, product_id="ETH-USD"
    )
    btc_min, btc_max, btc_cnt = ohlc_cache.cache_coverage(
        ohlc_cache.DAILY_GRANULARITY, product_id="BTC-USD"
    )
    assert eth_cnt == 2
    assert btc_cnt == 1
    assert eth_min == "2024-01-01T00:00:00Z"
    assert eth_max == "2024-01-02T00:00:00Z"
    assert btc_min == btc_max == "2024-01-01T00:00:00Z"

    eth_out = ohlc_cache.get_candles(
        ohlc_cache.DAILY_GRANULARITY, product_id="ETH-USD"
    )
    btc_out = ohlc_cache.get_candles(
        ohlc_cache.DAILY_GRANULARITY, product_id="BTC-USD"
    )
    assert len(eth_out) == 2
    assert len(btc_out) == 1
    assert btc_out[0]["close"] == 40_500.0
