"""Marked H4/H1/M15 structure board: chart render, path guard, hub payload."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import charts
import config
from dashboard.brain import _structure_board
from dashboard.charts import stance_chart_path
from patterns.htf_structure import HTFZone
from patterns.key_levels import KeyLevel

_START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _bars(count: int = 80) -> list[dict]:
    """Synthetic uptrending hourly bars (closes run 100.0 upward by 1.5)."""
    out = []
    for i in range(count):
        close = 100.0 + i * 1.5
        out.append(
            {
                "ts": (_START + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
                "open": close - 0.8,
                "high": close + 1.2,
                "low": close - 1.4,
                "close": close,
                "volume": 1000.0 + i,
            }
        )
    return out


def _zone() -> HTFZone:
    """Unmitigated bullish OB inside the synthetic bar range."""
    return HTFZone(
        zone_type="order_block",
        direction="bullish",
        low=140.0,
        high=150.0,
        start_ts=(_START + timedelta(hours=30)).isoformat().replace("+00:00", "Z"),
    )


class StanceChartRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._charts = Path(self._tmp.name) / "charts"
        self._patch = patch.object(config, "CHARTS_DIR", self._charts)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_render_writes_png_with_stable_name(self) -> None:
        path = Path(
            charts.render_stance_marked_chart(
                _bars(),
                product_id="BTC-USD",
                timeframe="M15",
                key_levels=[KeyLevel(150.0, "Daily Open", "#08bcd4")],
                htf_zones=[_zone()],
                stance="bullish",
                confidence=0.72,
            )
        )
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "stance_BTC_USD_M15_marked.png")
        self.assertGreater(path.stat().st_size, 0)

        # Re-rendering overwrites in place so the hub URL never changes.
        charts.render_stance_marked_chart(
            _bars(), product_id="BTC-USD", timeframe="M15", stance="bearish"
        )
        self.assertEqual(len(list(self._charts.glob("stance_*"))), 1)

    def test_render_draws_trading_log_overlays(self) -> None:
        """The board must use the same overlays as the trade journal charts."""
        near = KeyLevel(150.0, "Daily Open", "#08bcd4")
        far = KeyLevel(9000.0, "Yearly Open", "#ff0000")
        with patch.object(
            charts, "_draw_key_levels", wraps=charts._draw_key_levels
        ) as levels, patch.object(
            charts, "_draw_htf_zones", wraps=charts._draw_htf_zones
        ) as zones, patch.object(
            charts, "_draw_swing_hlines", wraps=charts._draw_swing_hlines
        ) as swings:
            charts.render_stance_marked_chart(
                _bars(),
                product_id="ETH-USD",
                timeframe="H4",
                key_levels=[near, far],
                htf_zones=[_zone()],
            )

        swings.assert_called_once()
        self.assertEqual(zones.call_args.args[2], [_zone()])
        # Levels far outside the candle range are filtered before drawing.
        self.assertEqual(levels.call_args.args[1], [near])

    def test_render_survives_missing_overlays(self) -> None:
        # A failed key-level/zone fetch still leaves a candle chart on the hub.
        path = charts.render_stance_marked_chart(
            _bars(12), product_id="ETH-USD", timeframe="H1"
        )
        self.assertTrue(Path(path).is_file())

    def test_stance_chart_path_rejects_unknown_timeframe(self) -> None:
        charts.render_stance_marked_chart(
            _bars(), product_id="ETH-USD", timeframe="H4"
        )
        self.assertIsNotNone(stance_chart_path("ETH-USD", "H4"))
        self.assertIsNotNone(stance_chart_path("ETH-USD", "h4"))
        self.assertIsNone(stance_chart_path("ETH-USD", "M5"))
        self.assertIsNone(stance_chart_path("ETH-USD", "../../etc/passwd"))
        self.assertIsNone(stance_chart_path("", "H4"))
        # Not rendered yet for BTC.
        self.assertIsNone(stance_chart_path("BTC-USD", "H4"))


class StructureBoardPayloadTests(StanceChartRenderTests):
    def test_board_pairs_charts_with_latest_stance(self) -> None:
        for tf in ("H4", "H1", "M15"):
            charts.render_stance_marked_chart(
                _bars(), product_id="BTC-USD", timeframe=tf
            )

        stances = [
            {
                "product_id": "BTC-USD",
                "timeframe": "H4",
                "stance": "neutral",
                "confidence": 0.3,
                "rationale": "programmatic",
                "created_at": "2026-08-10T22:00:12Z",
            },
            # Same cycle, LLM refine lands later and should win.
            {
                "product_id": "BTC-USD",
                "timeframe": "H4",
                "stance": "bearish",
                "confidence": 0.68,
                "rationale": "llm",
                "created_at": "2026-08-10T22:09:03Z",
            },
        ]
        board = _structure_board(stances)

        self.assertEqual(len(board), 1)
        self.assertEqual(board[0]["product_id"], "BTC-USD")
        self.assertEqual(board[0]["product_label"], "BTC")
        h4 = board[0]["charts"][0]
        self.assertEqual(h4["timeframe"], "H4")
        self.assertEqual(h4["stance"], "bearish")
        self.assertEqual(h4["rationale"], "llm")
        self.assertIn("/api/brain/structure/BTC-USD/H4", h4["url"])
        self.assertIn("v=2026-08-10T22%3A09%3A03Z".replace("%3A", ":"), h4["url"])

    def test_board_omits_products_without_charts(self) -> None:
        self.assertEqual(_structure_board([]), [])


if __name__ == "__main__":
    unittest.main()
