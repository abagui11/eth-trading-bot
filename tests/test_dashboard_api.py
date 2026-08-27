"""Tests for dashboard API — no subscriber data leaks."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard import data


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
        data.reset_spot_cache()

        from dashboard.app import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        data.reset_spot_cache()
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
        self.assertIn('data-tab="brain"', resp.text)
        self.assertIn('data-tab="trading"', resp.text)
        self.assertIn('data-tab="yield"', resp.text)
        self.assertIn('data-tab="mill"', resp.text)
        self.assertIn("Trade mill", resp.text)
        self.assertIn("live clip (internal)", resp.text)
        self.assertIn('id="tab-mill"', resp.text)
        self.assertIn("HQ live book", resp.text)
        self.assertIn("/api/trades/live?source=mill", resp.text)

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

    def test_feed_page_is_public(self) -> None:
        resp = self.client.get("/feed")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Idea feed", resp.text)
        self.assertIn("Viewing", resp.text)
        self.assertNotIn("secret_user", resp.text)

    def test_idea_stream_unauthenticated(self) -> None:
        resp = self.client.get("/api/ideas/stream")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["signed_in"])
        self.assertIn("ideas", data)

    def test_idea_decision_requires_session(self) -> None:
        resp = self.client.post(
            "/api/ideas/5/decision", json={"decision": "accept"}
        )
        self.assertEqual(resp.status_code, 401)


class IdeaFeedApiTests(unittest.TestCase):
    """Feed + decision API against a mill SQLite (the public stream)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmpdir.name)
        self._db = root / "ledger.db"
        self._ideas = root / "ideas.db"
        self._charts = root / "charts"
        self._charts.mkdir()

        sqlite3.connect(self._db).close()
        conn = sqlite3.connect(self._ideas)
        conn.executescript(
            """
            CREATE TABLE ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                product_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                title TEXT NOT NULL,
                blurb TEXT NOT NULL,
                signal_key TEXT NOT NULL UNIQUE,
                stance_context TEXT,
                confidence REAL,
                meta_json TEXT,
                status TEXT NOT NULL DEFAULT 'offered',
                entry REAL,
                stop_loss REAL,
                take_profits_json TEXT,
                risk_reward REAL,
                chart_path TEXT,
                sent_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idea_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                UNIQUE (idea_id, user_id)
            );
            INSERT INTO ideas
                (id, source, product_id, direction, title, blurb, signal_key,
                 entry, stop_loss, take_profits_json, created_at)
            VALUES (5, 'news', 'ETH-USD', 'long', 'Fixture', 'blurb', 'news:1',
                    100.0, 95.0, '[110.0]', '2026-08-27T00:00:00Z');
            """
        )
        conn.commit()
        conn.close()

        import config

        self._patches = [
            patch.object(config, "LEDGER_DB", self._db),
            patch.object(config, "CHARTS_DIR", self._charts),
            patch.object(config, "ROOT_DIR", root),
            patch.object(config, "ME_TOKEN_SECRET", "test-secret"),
            patch.object(config, "PAYWALL_ENABLED", False),
            patch.dict("os.environ", {"IDEAS_DB": str(self._ideas)}, clear=False),
        ]
        for item in self._patches:
            item.start()
        data.reset_spot_cache()
        from dashboard.app import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        data.reset_spot_cache()
        self.client.close()
        for item in reversed(self._patches):
            item.stop()
        self._tmpdir.cleanup()

    def test_stream_returns_sized_cards(self) -> None:
        resp = self.client.get("/api/ideas/stream")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(len(body["ideas"]), 1)
        self.assertEqual(body["ideas"][0]["id"], 5)
        self.assertEqual(body["ideas"][0]["product_id"], "ETH-USD")
        self.assertFalse(body["signed_in"])

    def test_funnel_endpoint(self) -> None:
        resp = self.client.get("/api/ideas/funnel")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertIn("minted", body)
        self.assertIn("telegram_sent", body)
        self.assertIn("not_on_telegram", body)

    def test_accept_via_magic_link_cookie(self) -> None:
        import user_books

        user_books.init_db()
        token = user_books.create_me_token(4242, ttl_sec=600)
        page = self.client.get(f"/feed?t={token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Signed in", page.text)
        self.assertIn("me_session", page.cookies)

        resp = self.client.post(
            "/api/ideas/5/decision", json={"decision": "accept"}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "recorded")
        self.assertEqual(body["decision"], "accept")

        stream = self.client.get("/api/ideas/stream").json()
        self.assertEqual(stream["ideas"][0]["my_decision"], "accept")

        dup = self.client.post(
            "/api/ideas/5/decision", json={"decision": "reject"}
        )
        self.assertEqual(dup.status_code, 200)
        self.assertEqual(dup.json()["status"], "duplicate")

    def test_vault_snapshot_and_empty_stream(self) -> None:
        snap = self.client.get("/api/vault/snapshot")
        self.assertEqual(snap.status_code, 200)
        body = snap.json()
        self.assertIn("policy", body)
        self.assertEqual(body["policy"]["notional_per_name_usd"], 1000.0)
        stream = self.client.get("/api/vault/stream")
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.json()["ideas"], [])


if __name__ == "__main__":
    unittest.main()
