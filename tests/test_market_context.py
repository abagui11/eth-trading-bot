"""Tests for market context recency and retest messaging."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import config
from patterns.market_context import _filter_recent_sfps, build_market_context
from patterns.sfp import SFPEvent


def _bar(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 100.0}


def _h1_series(hours: int, start_price: float = 1570.0) -> list[dict]:
    base = datetime(2026, 6, 30, 0, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(hours):
        ts = (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p = start_price + (i % 5) - 2
        bars.append(_bar(ts, p, p + 10, p - 10, p))
    return bars


class MarketContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_path = self._tmpdir.name + "/ledger.db"
        self._patch = patch.object(config, "LEDGER_DB", self._db_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_filter_recent_sfps_excludes_old_events(self) -> None:
        now = datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc)
        old = SFPEvent(
            ts="2026-06-28T23:00:00Z",
            bar_idx=10,
            timeframe="H1",
            direction="bullish",
            swept_level=1560.0,
            sweep_depth_pct=0.2,
            aligns_prior_swing=False,
            volume_spike=False,
            outcome_a="reversal",
            outcome_b=True,
            outcome_c=True,
        )
        fresh = SFPEvent(
            ts="2026-06-30T16:00:00Z",
            bar_idx=50,
            timeframe="H1",
            direction="bearish",
            swept_level=1620.0,
            sweep_depth_pct=0.2,
            aligns_prior_swing=False,
            volume_spike=False,
            outcome_a="reversal",
            outcome_b=True,
            outcome_c=True,
        )
        recent = _filter_recent_sfps([old, fresh], now=now)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].direction, "bearish")

    def test_context_includes_spot_and_decision_rules(self) -> None:
        h1 = _h1_series(30)
        h1[-3]["high"] = 1625.0
        h1[-3]["close"] = 1620.0
        h1[-1]["close"] = 1575.0
        h1[-1]["high"] = 1580.0

        h12 = []
        base = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        price = 2200.0
        for i in range(60):
            ts = (base + timedelta(hours=12 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            price = max(1500.0, price - 8)
            h12.append(_bar(ts, price, price + 20, price - 20, price - 5))

        ctx = build_market_context(h12, h1, h1)
        self.assertIn("Current spot:", ctx.summary_text)
        self.assertIn("do NOT say price has not reached the retest zone", ctx.summary_text)


if __name__ == "__main__":
    unittest.main()
