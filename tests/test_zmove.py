"""Z-move scoring and cooldown helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import zmove


def test_price_z_detects_planted_spike():
    # Noisy baseline then a large jump
    closes = [100.0]
    for i in range(170):
        closes.append(closes[-1] * (1.0 + 0.001 + ((i % 7) - 3) * 0.00015))
    closes.append(closes[-1] * 1.05)  # +5% spike

    scored = zmove.compute_price_z(closes, lookback=168)
    assert scored is not None
    z, ret, _mean, _std = scored
    assert ret > 0.04
    assert z >= 2.0


def test_price_z_sub_threshold_quiet_series():
    closes = [100.0]
    for i in range(170):
        closes.append(closes[-1] * (1.0 + 0.001 + ((i % 5) - 2) * 0.0002))
    # Mild continuation, not a spike
    closes.append(closes[-1] * 1.0012)

    scored = zmove.compute_price_z(closes, lookback=168)
    assert scored is not None
    z, _ret, _mean, _std = scored
    assert abs(z) < 2.0


def test_evaluate_bars_emits_price_and_volume_signals():
    bars = []
    price = 100.0
    for i in range(170):
        vol = 100.0 + (i % 11)
        bars.append(
            {
                "ts": f"2024-01-01T{i % 24:02d}:00:00Z",
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": vol,
            }
        )
        price *= 1.0 + 0.001 + ((i % 7) - 3) * 0.00015
    # Spike last bar (price + volume)
    bars.append(
        {
            "ts": "2024-01-08T00:00:00Z",
            "open": price,
            "high": price * 1.06,
            "low": price,
            "close": price * 1.05,
            "volume": 800.0,
        }
    )
    signals = zmove.evaluate_bars(
        bars,
        product_id="ETH-USD",
        lookback=168,
        threshold=2.0,
    )
    metrics = {s.metric for s in signals}
    assert "price" in metrics
    assert "volume" in metrics


def test_cooldown_suppresses_duplicate_fire(tmp_path: Path, monkeypatch):
    db = tmp_path / "ledger.db"
    monkeypatch.setattr("config.LEDGER_DB", db)
    monkeypatch.setattr(zmove.config, "LEDGER_DB", db)

    signal = zmove.ZMoveSignal(
        product_id="ETH-USD",
        metric="price",
        z=3.0,
        bar_ts="2024-01-01T00:00:00Z",
        value=0.05,
        mean=0.001,
        std=0.01,
        pct_move=5.0,
    )

    with (
        patch.object(zmove.bot_config, "ZMOVE_ENABLED", True),
        patch.object(zmove.bot_config, "ZMOVE_PRODUCT_ID", "ETH-USD"),
        patch.object(zmove.bot_config, "ZMOVE_LOOKBACK_H", 168),
        patch.object(zmove.bot_config, "ZMOVE_THRESHOLD", 2.0),
        patch.object(zmove.bot_config, "ZMOVE_COOLDOWN_SEC", 7200),
        patch.object(zmove.research, "fetch_h1_bars", return_value=[]),
        patch.object(zmove, "evaluate_bars", return_value=[signal]),
        patch.object(zmove.notify, "broadcast_plain_text") as broadcast,
    ):
        # Force enough bars path by returning signal from evaluate; fetch can be empty
        # but evaluate is patched — still need fetch to not raise. OK.
        with patch.object(
            zmove.research,
            "fetch_h1_bars",
            return_value=[{"ts": "x", "close": 1, "volume": 1}] * 180,
        ):
            first = zmove.run_zmove_scan()
            second = zmove.run_zmove_scan()

    assert len(first) == 1
    assert second == []
    assert broadcast.call_count == 1
