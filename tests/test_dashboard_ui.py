"""Basic UI structure / CSS smoke checks for the trade journal."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


def _sample_trade(**overrides):
    base = {
        "side": "long",
        "action": "deriv_buy",
        "entry": 3200.0,
        "avg_entry": 3200.0,
        "exit": 3300.0,
        "spot": 3250.0,
        "pnl_usd": 50.0,
        "pnl_pct": 3.1,
        "is_winner": True,
        "opened_at": "2026-07-14T16:00:00Z",
        "closed_at": "2026-07-14T18:00:00Z",
        "close_reason": "take_profit",
        "open_cycle_id": "20260714T160000Z",
        "close_cycle_id": "20260714T180000Z",
        "stop_loss": 3100.0,
        "take_profits": [3300.0],
        "risk_reward": 2.0,
        "eth_qty": 0.5,
        "order_block": None,
        "setup_tags": ["h4_ob"],
        "rationale": "Test rationale for structure.",
        "structure_chart_url": "/api/chart/x?kind=structure&tf=H4",
        "execution_chart_url": "/api/chart/x?kind=entry&tf=M5",
        "thumb_chart_url": "/api/chart/x?kind=entry&tf=M5",
        "dist_to_sl_pct": 2.0,
        "dist_to_tp_pct": 1.0,
        "unrealized_pnl_usd": 25.0,
    }
    base.update(overrides)
    return base


class DashboardUiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmpdir.name)
        self._db = root / "ledger.db"
        self._charts = root / "charts"
        self._charts.mkdir()
        self._db.write_bytes(b"")

        # Minimal schema via paper/ledger init after LEDGER_DB patch.
        self._patches = [
            patch.object(config, "LEDGER_DB", self._db),
            patch.object(config, "CHARTS_DIR", self._charts),
            patch.object(config, "ROOT_DIR", root),
            patch("dashboard.data.research.get_spot_price", return_value=2000.0),
            patch(
                "dashboard.data.get_status_payload",
                return_value={
                    "spot": 2000.0,
                    "headline": "Flat",
                    "alerts": [],
                    "watching": [],
                    "phase": "idle",
                    "ts": None,
                    "cycle_id": None,
                    "chart_read_score": None,
                    "score_badge": "none",
                    "h4_chart_url": "/api/chart/latest",
                },
            ),
            patch(
                "dashboard.data.get_performance_payload",
                return_value={
                    "equity_usd": 5000.0,
                    "total_pnl_usd": 0.0,
                    "total_pnl_pct": 0.0,
                    "win_rate_pct": 0.0,
                    "starting_usd": 5000.0,
                    "closed_trade_count": 1,
                    "open_count": 1,
                    "chart_read": {"avg_score_30d": None, "issue_rate_pct": 0},
                    "epoch": {
                        "epoch_label": "5k_usd",
                        "epoch_started_at": None,
                    },
                },
            ),
            patch(
                "dashboard.data.get_open_positions_payload",
                return_value=[_sample_trade(exit=None, close_reason=None, status="open")],
            ),
            patch(
                "dashboard.data.get_closed_trades_payload",
                return_value=[_sample_trade()],
            ),
            patch("dashboard.data.get_archived_trades_payload", return_value=[]),
            patch("dashboard.data.get_cycles", return_value=[]),
            patch(
                "dashboard.data.get_macro_payload",
                return_value={
                    "enabled": False,
                    "posture": {},
                    "monitored_sources": [],
                    "active": [],
                    "recent": [],
                },
            ),
        ]
        for p in self._patches:
            p.start()

        from dashboard.app import create_app
        from fastapi.testclient import TestClient

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        for p in reversed(self._patches):
            p.stop()
        self._tmpdir.cleanup()

    def test_trade_cards_start_collapsed(self) -> None:
        html = self.client.get("/").text
        self.assertIn('class="trade-card trade-live"', html)
        self.assertIn('class="trade-card"', html)
        # No card should be pre-opened.
        self.assertNotIn("<details open", html)
        self.assertNotIn('<details class="trade-card" open', html)
        self.assertIn('class="trade-summary-inner"', html)
        self.assertIn('class="trade-title"', html)
        self.assertIn("Jul 14 [long]", html)
        self.assertIn("4:00 PM", html)  # opened_at 16:00Z
        self.assertNotIn("2026-07-14T16:00", html)
        self.assertIn('class="trade-body"', html)
        self.assertIn('title="Long position', html)
        # Exactly as many trade cards as details wrappers.
        n_details = len(re.findall(r"<details\s+class=\"trade-card", html))
        n_bodies = len(re.findall(r'class="trade-body"', html))
        self.assertEqual(n_details, 2)
        self.assertEqual(n_bodies, 2)

    def test_css_forces_collapse_and_image_caps(self) -> None:
        css = self.client.get("/static/style.css").text
        self.assertIn(".trade-card:not([open]) > .trade-body", css)
        self.assertRegex(css, r"\.trade-card:not\(\[open\]\)\s*>\s*\.trade-body\s*\{[^}]*display:\s*none")
        # Flex belongs on the inner wrapper, not on summary.
        self.assertIn(".trade-summary-inner", css)
        self.assertRegex(css, r"\.trade-summary\s*\{[^}]*display:\s*block")
        self.assertIn("max-height: 280px", css)
        self.assertIn("max-width: 100%", css)
        self.assertIn(".macro-scroll", css)
        self.assertRegex(css, r"\.macro-scroll\s*\{[^}]*max-height:\s*220px")
        # Guard against regressing to flex-on-summary.
        self.assertNotRegex(
            css,
            r"\.trade-summary\s*\{[^}]*display:\s*flex",
        )


if __name__ == "__main__":
    unittest.main()
