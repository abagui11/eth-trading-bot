"""Tests for dashboard API — no subscriber data leaks."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class DashboardApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmpdir.name)
        self._db = root / "ledger.db"
        self._charts = root / "charts"
        self._charts.mkdir()

        conn = sqlite3.connect(self._db)
        conn.executescript(
            """
            CREATE TABLE suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, cycle_id TEXT, action TEXT, size REAL, entry REAL,
                stop_loss REAL, take_profits TEXT, risk_reward REAL,
                price_at_suggestion REAL, rationale TEXT, chart_path TEXT,
                setup_tags TEXT
            );
            CREATE TABLE paper_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                starting_usd REAL, cash_usd REAL, last_cycle_id TEXT, last_spot REAL
            );
            INSERT INTO paper_state VALUES (1, 1000, 1000, NULL, 2000);
            CREATE TABLE paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                open_cycle_id TEXT, opened_at TEXT, side TEXT, action TEXT,
                eth_qty REAL, avg_entry REAL, stop_loss REAL, take_profits TEXT,
                risk_reward REAL, suggested_size REAL, status TEXT
            );
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, cycle_id TEXT, event TEXT, side TEXT, eth_qty REAL,
                price REAL, cash_usd REAL, equity_usd REAL,
                position_id INTEGER, close_reason TEXT
            );
            CREATE TABLE audit_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, cycle_id TEXT UNIQUE, spot REAL,
                snapshot_json TEXT, suggestion_json TEXT,
                marked_chart_paths TEXT, market_context_summary TEXT
            );
            CREATE TABLE audit_verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, cycle_id TEXT, source TEXT, user_id INTEGER,
                deterministic_json TEXT, llm_json TEXT, has_issues INTEGER,
                llm_verified_json TEXT, score INTEGER, score_breakdown_json TEXT
            );
            CREATE TABLE subscribers (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT, active INTEGER, last_seen TEXT
            );
            INSERT INTO subscribers VALUES (999, 'secret_user', 1, 'now');
            INSERT INTO suggestions (
                ts, cycle_id, action, take_profits, price_at_suggestion, rationale, chart_path
            ) VALUES (
                '2026-07-02T12:00:00Z', '20260702T120000Z', 'no_trade', '[]',
                2000.0, 'Waiting for setup', ''
            );
            """
        )
        marked = str(self._charts / "20260702T120000Z_H4_marked.png")
        Path(marked).write_bytes(b"\x89PNG\r\n")
        snap = {
            "spot": 2000.0,
            "alerts": ["Test alert"],
            "setup_state": {"phase": "idle"},
            "zone_snapshot": {},
            "order_blocks": [],
        }
        conn.execute(
            """
            INSERT INTO audit_snapshots (
                ts, cycle_id, spot, snapshot_json, suggestion_json,
                marked_chart_paths, market_context_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-07-02T12:00:00Z",
                "20260702T120000Z",
                2000.0,
                json.dumps(snap),
                json.dumps({"action": "no_trade"}),
                json.dumps({"H4": marked}),
                "",
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_verdicts (
                ts, cycle_id, source, deterministic_json, llm_json, has_issues,
                score, score_breakdown_json, llm_verified_json
            ) VALUES (?, ?, 'hourly', '[]', '[]', 0, 95, '{}', '[]')
            """,
            ("2026-07-02T12:00:00Z", "20260702T120000Z"),
        )
        conn.commit()
        conn.close()

        import config

        self._config_db = patch.object(config, "LEDGER_DB", self._db)
        self._config_charts = patch.object(config, "CHARTS_DIR", self._charts)
        self._config_root = patch.object(config, "ROOT_DIR", root)
        self._config_db.start()
        self._config_charts.start()
        self._config_root.start()

        self._spot = patch(
            "dashboard.data.research.get_spot_prices",
            return_value={"ETH-USD": 2000.0, "BTC-USD": 60000.0},
        )
        self._spot.start()

        from dashboard.app import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self._spot.stop()
        self._config_root.stop()
        self._config_charts.stop()
        self._config_db.stop()
        self.client.close()
        self._tmpdir.cleanup()

    def test_index_ok(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("secret_user", resp.text)
        self.assertNotIn("subscribers", resp.text.lower())

    def test_api_status_includes_score(self) -> None:
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["chart_read_score"], 95)
        self.assertEqual(data["eth_spot"], 2000.0)
        self.assertEqual(data["btc_spot"], 60000.0)
        self.assertEqual(
            data["spots"],
            {"ETH-USD": 2000.0, "BTC-USD": 60000.0},
        )
        self.assertTrue(data["score_tooltip"])
        self.assertIn("headline", data)

    def test_api_spot_keeps_legacy_and_dual_asset_shape(self) -> None:
        resp = self.client.get("/api/spot")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["spot"], 2000.0)
        self.assertEqual(data["eth"], 2000.0)
        self.assertEqual(data["btc"], 60000.0)

    def test_chart_endpoint(self) -> None:
        resp = self.client.get("/api/chart/20260702T120000Z")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/png")

    def test_no_subscriber_api(self) -> None:
        resp = self.client.get("/api/subscribers")
        self.assertEqual(resp.status_code, 404)

    def test_me_requires_token(self) -> None:
        resp = self.client.get("/me")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Telegram", resp.text)

    def test_me_with_valid_token(self) -> None:
        import user_books

        user_books.init_db()
        user_books.open_paper_account(4242, 1000.0, "dashuser")
        token = user_books.create_me_token(4242, ttl_sec=600)
        resp = self.client.get(f"/me?t={token}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("My book", resp.text)
        self.assertIn("$1000", resp.text)
        self.assertIn("me_session", resp.cookies)


if __name__ == "__main__":
    unittest.main()
