"""Tests for the BTC 4-year-cycle long thesis module."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import config
from intelligence import store
from intelligence.cycle_thesis import (
    _fallback_thesis,
    current_phase,
    render_cycle_chart,
    run_long_thesis_refresh,
)


def _daily_bars(n: int, start_price: float = 20_000.0) -> list[dict]:
    bars = []
    for i in range(n):
        price = start_price * (1 + 0.001 * i)
        bars.append(
            {
                "ts": f"20{20 + i // 365:02d}-01-01T00:00:00Z",
                "open": price,
                "high": price * 1.02,
                "low": price * 0.98,
                "close": price,
                "volume": 1000.0,
            }
        )
    return bars


class TestCurrentPhase(unittest.TestCase):
    def test_just_after_2024_halving_is_accumulation(self) -> None:
        phase, days = current_phase(date(2024, 5, 20))
        self.assertEqual(phase, "post_halving_accumulation")
        self.assertEqual(days, 30)

    def test_bull_expansion_window(self) -> None:
        phase, _ = current_phase(date(2025, 2, 1))
        self.assertEqual(phase, "bull_expansion")

    def test_top_window(self) -> None:
        phase, _ = current_phase(date(2025, 11, 15))
        self.assertEqual(phase, "cycle_top_window")

    def test_bear_drawdown(self) -> None:
        phase, _ = current_phase(date(2026, 8, 10))
        self.assertEqual(phase, "bear_drawdown")


class TestFallbackThesis(unittest.TestCase):
    def test_bias_maps_from_phase(self) -> None:
        thesis = _fallback_thesis("bull_expansion", 300, {"available": False})
        self.assertEqual(thesis["bias"], "bullish")
        thesis = _fallback_thesis("bear_drawdown", 900, {"available": False})
        self.assertEqual(thesis["bias"], "bearish")


class TestLongThesisRefresh(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self._orig_db = config.LEDGER_DB
        self._orig_charts = config.CHARTS_DIR
        config.LEDGER_DB = root / "ledger.db"
        config.CHARTS_DIR = root / "charts"
        store.init_db()

    def tearDown(self) -> None:
        config.LEDGER_DB = self._orig_db
        config.CHARTS_DIR = self._orig_charts
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_render_cycle_chart_writes_png(self) -> None:
        bars = _daily_bars(400)
        path = render_cycle_chart(bars, phase="bull_expansion", days_since_halving=300)
        self.assertTrue(Path(path).exists())
        self.assertTrue(path.endswith("_btc_cycle.png"))

    def test_refresh_persists_and_is_idempotent_per_day(self) -> None:
        bars = _daily_bars(400)
        with mock.patch(
            "intelligence.cycle_thesis.fetch_btc_history", return_value=bars
        ), mock.patch(
            "intelligence.cycle_thesis.compute_gold_ratios",
            return_value={"available": False},
        ), mock.patch("anthropic.Anthropic") as anthropic_cls:
            anthropic_cls.return_value.messages.create.side_effect = RuntimeError(
                "api down"
            )
            first = run_long_thesis_refresh()
            self.assertIn("thesis", first)
            self.assertIn(first["thesis"]["bias"], ("bullish", "neutral", "bearish"))

            # Second call same day: served from store, no re-render.
            with mock.patch(
                "intelligence.cycle_thesis.render_cycle_chart"
            ) as render_mock:
                second = run_long_thesis_refresh()
                render_mock.assert_not_called()
            self.assertEqual(second["as_of_date"], first["as_of_date"])

        stored = store.latest_long_thesis()
        self.assertIsNotNone(stored)
        self.assertIn("days_since_halving", stored["thesis"])


if __name__ == "__main__":
    unittest.main()
