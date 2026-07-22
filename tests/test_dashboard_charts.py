"""Tests for dashboard trade-chart resolution and enrichment."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from dashboard.charts import (
    convention_chart_path,
    outcome_filenames,
    resolve_trade_chart,
    trade_chart_urls,
)


class DashboardChartResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmpdir.name)
        self._charts = root / "charts"
        self._charts.mkdir()
        self._patch_charts = patch.object(config, "CHARTS_DIR", self._charts)
        self._patch_root = patch.object(config, "ROOT_DIR", root)
        self._patch_charts.start()
        self._patch_root.start()

    def tearDown(self) -> None:
        self._patch_root.stop()
        self._patch_charts.stop()
        self._tmpdir.cleanup()

    def test_convention_and_outcome_filenames(self) -> None:
        cycle = "20260714T120000Z"
        names = outcome_filenames(cycle)
        self.assertEqual(names["H4"], f"{cycle}_H4_outcome.png")
        self.assertEqual(names["M5"], f"{cycle}_M5_outcome.png")

        path = self._charts / names["H4"]
        path.write_bytes(b"\x89PNG\r\n")
        resolved = convention_chart_path(cycle, "H4", "outcome")
        self.assertEqual(resolved, path.resolve())

    def test_resolve_prefers_convention_over_ledger(self) -> None:
        cycle = "20260714T130000Z"
        entry = self._charts / f"{cycle}_M5_entry.png"
        entry.write_bytes(b"\x89PNG\r\n")
        other = self._charts / f"{cycle}_M5_annotated.png"
        other.write_bytes(b"\x89PNG\r\n")
        path = resolve_trade_chart(
            cycle,
            kind="entry",
            tf="M5",
            ledger_chart_path=str(other),
        )
        self.assertEqual(path, entry.resolve())

    def test_trade_chart_urls_prefer_outcome_when_closed(self) -> None:
        cycle = "20260714T140000Z"
        (self._charts / f"{cycle}_H4_outcome.png").write_bytes(b"\x89PNG\r\n")
        (self._charts / f"{cycle}_M5_outcome.png").write_bytes(b"\x89PNG\r\n")
        urls = trade_chart_urls(cycle, closed=True)
        self.assertEqual(
            urls["structure_chart_url"],
            f"/api/chart/{cycle}?kind=outcome&tf=H4",
        )
        self.assertEqual(
            urls["execution_chart_url"],
            f"/api/chart/{cycle}?kind=outcome&tf=M5",
        )
        self.assertEqual(urls["thumb_chart_url"], urls["execution_chart_url"])

    def test_trade_chart_urls_open_uses_structure_entry(self) -> None:
        cycle = "20260714T150000Z"
        (self._charts / f"{cycle}_H4_structure.png").write_bytes(b"\x89PNG\r\n")
        (self._charts / f"{cycle}_M5_entry.png").write_bytes(b"\x89PNG\r\n")
        urls = trade_chart_urls(cycle, closed=False)
        self.assertEqual(
            urls["structure_chart_url"],
            f"/api/chart/{cycle}?kind=structure&tf=H4",
        )
        self.assertEqual(
            urls["execution_chart_url"],
            f"/api/chart/{cycle}?kind=entry&tf=M5",
        )

    def test_convention_chart_path_accepts_broadcast_slug(self) -> None:
        cycle = "20260722T110000Z_BTC"
        path = self._charts / f"{cycle}_BTC_USD_M5_entry.png"
        path.write_bytes(b"\x89PNG\r\n")
        resolved = convention_chart_path(cycle, "M5", "entry")
        self.assertEqual(resolved, path.resolve())


class DashboardEnrichmentTests(unittest.TestCase):
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
            CREATE TABLE audit_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, cycle_id TEXT UNIQUE, spot REAL,
                snapshot_json TEXT, suggestion_json TEXT,
                marked_chart_paths TEXT, market_context_summary TEXT
            );
            """
        )
        cycle = "20260714T160000Z"
        structure = self._charts / f"{cycle}_H4_structure.png"
        entry = self._charts / f"{cycle}_M5_entry.png"
        structure.write_bytes(b"\x89PNG\r\n")
        entry.write_bytes(b"\x89PNG\r\n")
        conn.execute(
            """
            INSERT INTO suggestions (
                ts, cycle_id, action, size, entry, stop_loss, take_profits,
                risk_reward, price_at_suggestion, rationale, chart_path, setup_tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-07-14T16:00:00Z",
                cycle,
                "deriv_buy",
                0.5,
                3200.0,
                3100.0,
                json.dumps([3300.0, 3400.0]),
                2.5,
                3200.0,
                "H4 bullish OB retest into M5 discount.",
                f"{structure},{entry}",
                "h4_ob,m5_fib",
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_snapshots (
                ts, cycle_id, spot, snapshot_json, suggestion_json,
                marked_chart_paths, market_context_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-07-14T16:00:00Z",
                cycle,
                3200.0,
                json.dumps({"spot": 3200.0, "order_blocks": [], "htf_zones": [], "key_levels_near": []}),
                json.dumps(
                    {
                        "action": "deriv_buy",
                        "order_block": {
                            "low": 3180.0,
                            "high": 3220.0,
                            "start_ts": "2026-07-14T12:00:00Z",
                            "end_ts": "2026-07-14T14:00:00Z",
                        },
                        "structure_chart": "H4",
                        "entry_chart": "M5",
                        "rationale": "H4 bullish OB retest into M5 discount.",
                    }
                ),
                "{}",
                "",
            ),
        )
        conn.commit()
        conn.close()
        self._cycle = cycle

        self._patches = [
            patch.object(config, "LEDGER_DB", self._db),
            patch.object(config, "CHARTS_DIR", self._charts),
            patch.object(config, "ROOT_DIR", root),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmpdir.cleanup()

    def test_enrich_open_position(self) -> None:
        from dashboard.data import enrich_open_position

        pos = {
            "open_cycle_id": self._cycle,
            "opened_at": "2026-07-14T16:05:00Z",
            "side": "long",
            "action": "deriv_buy",
            "eth_qty": 0.5,
            "avg_entry": 3200.0,
            "stop_loss": 3100.0,
            "take_profits": [3300.0, 3400.0],
            "risk_reward": 2.5,
            "spot": 3250.0,
            "unrealized_pnl_usd": 25.0,
        }
        enriched = enrich_open_position(pos)
        self.assertEqual(enriched["status"], "open")
        self.assertIn("H4 bullish OB", enriched["rationale"])
        self.assertEqual(enriched["setup_tags"], ["h4_ob", "m5_fib"])
        self.assertIsNotNone(enriched["order_block"])
        self.assertIsNotNone(enriched["structure_chart_url"])
        self.assertIsNotNone(enriched["execution_chart_url"])
        self.assertTrue(enriched["is_winner"])
        self.assertIsNotNone(enriched["dist_to_sl_pct"])
        self.assertIsNotNone(enriched["dist_to_tp_pct"])

    def test_enrich_closed_trade(self) -> None:
        from dashboard.data import enrich_closed_trade

        # Prefer outcomes when present.
        (self._charts / f"{self._cycle}_H4_outcome.png").write_bytes(b"\x89PNG\r\n")
        (self._charts / f"{self._cycle}_M5_outcome.png").write_bytes(b"\x89PNG\r\n")
        trade = {
            "side": "long",
            "open_cycle_id": self._cycle,
            "close_cycle_id": "20260714T180000Z",
            "eth_qty": 0.5,
            "entry": 3200.0,
            "exit": 3300.0,
            "opened_at": "2026-07-14T16:05:00Z",
            "closed_at": "2026-07-14T18:00:00Z",
            "close_reason": "take_profit",
            "realized_pnl_usd": 50.0,
            "realized_pnl_pct": 3.125,
        }
        enriched = enrich_closed_trade(trade)
        self.assertEqual(enriched["status"], "closed")
        self.assertIn("kind=outcome", enriched["structure_chart_url"])
        self.assertIn("kind=outcome", enriched["execution_chart_url"])
        self.assertEqual(enriched["stop_loss"], 3100.0)
        self.assertEqual(enriched["take_profits"], [3300.0, 3400.0])


class DashboardChartApiKindTests(unittest.TestCase):
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
            """
        )
        cycle = "20260714T170000Z"
        marked = self._charts / f"{cycle}_H4_marked.png"
        structure = self._charts / f"{cycle}_H4_structure.png"
        entry = self._charts / f"{cycle}_M5_entry.png"
        for p in (marked, structure, entry):
            p.write_bytes(b"\x89PNG\r\n")
        conn.execute(
            """
            INSERT INTO suggestions (
                ts, cycle_id, action, take_profits, price_at_suggestion, rationale, chart_path
            ) VALUES (?, ?, 'no_trade', '[]', 2000.0, 'Waiting', ?)
            """,
            ("2026-07-14T17:00:00Z", cycle, f"{structure},{entry}"),
        )
        conn.execute(
            """
            INSERT INTO audit_snapshots (
                ts, cycle_id, spot, snapshot_json, suggestion_json,
                marked_chart_paths, market_context_summary
            ) VALUES (?, ?, ?, '{}', '{}', ?, '')
            """,
            (
                "2026-07-14T17:00:00Z",
                cycle,
                2000.0,
                json.dumps({"H4": str(marked)}),
            ),
        )
        conn.commit()
        conn.close()
        self._cycle = cycle

        self._patches = [
            patch.object(config, "LEDGER_DB", self._db),
            patch.object(config, "CHARTS_DIR", self._charts),
            patch.object(config, "ROOT_DIR", root),
            patch(
                "dashboard.data.research.get_spot_prices",
                return_value={"ETH-USD": 2000.0, "BTC-USD": 60000.0},
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

    def test_chart_kind_structure_and_entry(self) -> None:
        resp = self.client.get(f"/api/chart/{self._cycle}?kind=structure&tf=H4")
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get(f"/api/chart/{self._cycle}?kind=entry&tf=M5")
        self.assertEqual(resp.status_code, 200)

    def test_chart_invalid_kind(self) -> None:
        resp = self.client.get(f"/api/chart/{self._cycle}?kind=nope&tf=H4")
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
