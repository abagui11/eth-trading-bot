"""Tests for market context recency and retest messaging."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import config
from patterns.market_context import _fib_position_label, _filter_recent_sfps, build_market_context
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


def _m5_series(bars: int = 60, start_price: float = 1570.0) -> list[dict]:
    """Fabricate M5 bars (5-minute spacing) for build_market_context tests."""
    base = datetime(2026, 6, 30, 0, 0, tzinfo=timezone.utc)
    out = []
    for i in range(bars):
        ts = (base + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p = start_price + (i % 5) - 2
        out.append(_bar(ts, p, p + 3, p - 3, p))
    return out


def _h4_series(bars: int = 60, start_price: float = 2200.0) -> list[dict]:
    base = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    out = []
    price = start_price
    for i in range(bars):
        ts = (base + timedelta(hours=4 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        price = max(1500.0, price - 8)
        out.append(_bar(ts, price, price + 20, price - 20, price - 5))
    return out


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
            timeframe="M5",
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
            timeframe="M5",
            direction="bearish",
            swept_level=1620.0,
            sweep_depth_pct=0.2,
            aligns_prior_swing=False,
            volume_spike=False,
            outcome_a="reversal",
            outcome_b=True,
            outcome_c=True,
        )
        recent = _filter_recent_sfps([old, fresh], now=now)[0]
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].direction, "bearish")

    def test_filter_excludes_live_invalidated_bearish_sfp(self) -> None:
        now = datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc)
        event = SFPEvent(
            ts="2026-06-30T12:00:00Z",
            bar_idx=12,
            timeframe="M5",
            direction="bearish",
            swept_level=1587.0,
            sweep_depth_pct=0.2,
            aligns_prior_swing=False,
            volume_spike=False,
            outcome_a="reversal",
            outcome_b=True,
            outcome_c=True,
        )
        valid, invalidated = _filter_recent_sfps([event], spot=1595.0, now=now)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(invalidated), 1)
        self.assertEqual(invalidated[0].swept_level, 1587.0)

    def test_context_includes_spot_and_decision_rules(self) -> None:
        h1 = _h1_series(30)
        h1[-3]["high"] = 1625.0
        h1[-3]["close"] = 1620.0
        h1[-1]["close"] = 1575.0
        h1[-1]["high"] = 1580.0
        h4 = _h4_series()
        m5 = _m5_series()
        m5[-3]["high"] = 1625.0
        m5[-3]["close"] = 1620.0
        m5[-1]["close"] = 1575.0
        m5[-1]["high"] = 1580.0

        ctx = build_market_context(h4, h1, m5)
        self.assertIn("Current spot:", ctx.summary_text)
        self.assertIn("do NOT say price has not reached the retest zone", ctx.summary_text)

    def test_fib_position_label_above(self) -> None:
        self.assertEqual(_fib_position_label(1630.0, 1620.0, 1625.0), "above")

    def test_summary_uses_retest_vocabulary(self) -> None:
        h1 = _h1_series(30)
        h4 = _h4_series(start_price=1800.0)
        m5 = _m5_series()
        ctx = build_market_context(h4, h1, m5)
        if "Retest status (rolling 24h)" in ctx.summary_text:
            self.assertIn("Retest status (rolling 24h)", ctx.summary_text)
        self.assertIn(
            "Setup phase name records workflow history",
            ctx.summary_text,
        )


if __name__ == "__main__":
    unittest.main()
