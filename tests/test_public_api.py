"""Tests for the eva.finance public API (dashboard/public_api.py).

Covers the beta signup path (persistence first, honeypot, rate limit,
fire-and-forget email) and the strategies aggregate math against a small
synthetic ledger. Everything runs on temp SQLite files; no network.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from dashboard import public_api


def _make_app() -> FastAPI:
    """A minimal app hosting only the public router — no dashboard deps."""
    app = FastAPI()
    app.include_router(public_api.router)
    return app


class PublicApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmpdir.name)
        self._db = root / "ledger.db"
        self._seed_ledger(self._db)
        self._patches = [
            patch.object(config, "LEDGER_DB", self._db),
            patch.object(config, "RESEND_API_KEY", None),
        ]
        for p in self._patches:
            p.start()
        # reset module-level state between tests
        public_api._rate_hits.clear()
        public_api._cache_payload = None
        public_api._cache_at = 0.0
        self.client = TestClient(_make_app())

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    @staticmethod
    def _seed_ledger(path: Path) -> None:
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE live_trades (
                    id INTEGER PRIMARY KEY, source TEXT, status TEXT,
                    realized_pnl_usd REAL, pnl_usd REAL,
                    opened_at TEXT, closed_at TEXT
                );
                CREATE TABLE paper_trades (
                    id INTEGER PRIMARY KEY, ts TEXT, equity_usd REAL
                );
                CREATE TABLE paper_positions (
                    id INTEGER PRIMARY KEY, status TEXT, tps_hit INTEGER
                );
                CREATE TABLE paper_state (starting_usd REAL);
                CREATE TABLE suggestions (
                    id INTEGER PRIMARY KEY, ts TEXT, action TEXT,
                    trigger_name TEXT
                );
                CREATE TABLE yield_nav_snapshots (
                    snapshot_date TEXT PRIMARY KEY, nav_usd REAL,
                    collateral_usd REAL, debt_usd REAL, pt_usd REAL,
                    health_factor REAL, created_at TEXT, eth_price_usd REAL
                );
                """
            )
            conn.executemany(
                "INSERT INTO live_trades"
                " (source, status, realized_pnl_usd, pnl_usd, opened_at, closed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("hq", "closed", 20.0, 20.0,
                     "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"),
                    ("hq", "closed", -10.0, -10.0,
                     "2026-09-03T00:00:00Z", "2026-09-03T12:00:00Z"),
                    ("hq", "open", None, None, "2026-09-04T00:00:00Z", None),
                    ("mill", "closed", -5.0, -5.0,
                     "2026-09-01T00:00:00Z", "2026-09-01T06:00:00Z"),
                ],
            )
            conn.executemany(
                "INSERT INTO paper_trades (ts, equity_usd) VALUES (?, ?)",
                [
                    ("2026-08-06T00:00:00Z", 5000.0),
                    ("2026-08-07T00:00:00Z", 5100.0),
                    ("2026-08-07T12:00:00Z", 5050.0),
                ],
            )
            conn.executemany(
                "INSERT INTO paper_positions (status, tps_hit) VALUES (?, ?)",
                [("closed", 2), ("closed", 0), ("open", 0)],
            )
            conn.execute("INSERT INTO paper_state (starting_usd) VALUES (5000.0)")
            conn.executemany(
                "INSERT INTO suggestions (ts, action, trigger_name) VALUES (?, ?, ?)",
                [
                    ("2026-09-01T00:00:00Z", "no_trade", None),
                    ("2026-09-01T01:00:00Z", "no_trade", None),
                    ("2026-09-01T02:00:00Z", "no_trade", ""),
                    ("2026-09-01T03:00:00Z", "spot_buy", None),
                    # watchdog rows must not count against the hourly stats
                    ("2026-09-01T03:30:00Z", "spot_sell", "m5_ob_fib_short"),
                ],
            )
            conn.executemany(
                "INSERT INTO yield_nav_snapshots"
                " (snapshot_date, nav_usd, created_at) VALUES (?, ?, ?)",
                [
                    ("2026-08-26", 4000.0, "2026-08-26T00:00:00Z"),
                    ("2026-08-27", 4040.0, "2026-08-27T00:00:00Z"),
                ],
            )


class BetaSignupTests(PublicApiTestBase):
    def test_signup_persists_row(self) -> None:
        res = self.client.post(
            "/api/public/beta",
            json={"email": "Investor@Example.com", "name": "Pat", "note": "hi"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True})
        with sqlite3.connect(self._db) as conn:
            rows = conn.execute(
                "SELECT email, name, note FROM beta_signups"
            ).fetchall()
        self.assertEqual(rows, [("investor@example.com", "Pat", "hi")])

    def test_honeypot_accepts_but_stores_nothing(self) -> None:
        res = self.client.post(
            "/api/public/beta",
            json={"email": "bot@spam.com", "website": "http://spam"},
        )
        self.assertEqual(res.status_code, 200)
        with sqlite3.connect(self._db) as conn:
            conn.executescript(public_api._BETA_SCHEMA)
            n = conn.execute("SELECT COUNT(*) FROM beta_signups").fetchone()[0]
        self.assertEqual(n, 0)

    def test_invalid_email_rejected(self) -> None:
        res = self.client.post("/api/public/beta", json={"email": "not-an-email"})
        self.assertEqual(res.status_code, 422)

    def test_rate_limit(self) -> None:
        for i in range(public_api._RATE_MAX):
            res = self.client.post(
                "/api/public/beta", json={"email": f"a{i}@b.co"}
            )
            self.assertEqual(res.status_code, 200)
        res = self.client.post("/api/public/beta", json={"email": "z@b.co"})
        self.assertEqual(res.status_code, 429)

    def test_email_failure_never_fails_signup(self) -> None:
        with patch.object(config, "RESEND_API_KEY", "key"), patch(
            "requests.post", side_effect=RuntimeError("smtp down")
        ):
            res = self.client.post(
                "/api/public/beta", json={"email": "x@y.co"}
            )
        self.assertEqual(res.status_code, 200)
        with sqlite3.connect(self._db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM beta_signups").fetchone()[0]
        self.assertEqual(n, 1)

    def test_notify_sends_one_request_per_recipient(self) -> None:
        """Resend 403s the whole call if any recipient is undeliverable.

        Batching both operators into one request means an unverified sender
        silently notifies nobody, so each recipient gets its own request.
        """
        with patch.object(config, "RESEND_API_KEY", "key"), patch.object(
            config, "BETA_SIGNUP_EMAIL_TO", "a@op.co,b@op.co"
        ), patch("requests.post") as post:
            post.return_value.status_code = 200
            self.client.post("/api/public/beta", json={"email": "x@y.co"})

        self.assertEqual(post.call_count, 2)
        sent_to = [c.kwargs["json"]["to"] for c in post.call_args_list]
        self.assertEqual(sent_to, [["a@op.co"], ["b@op.co"]])

    def test_one_rejected_recipient_still_notifies_the_other(self) -> None:
        def respond(*_args, **kwargs):
            res = unittest.mock.Mock()
            rejected = kwargs["json"]["to"] == ["b@op.co"]
            res.status_code = 403 if rejected else 200
            res.text = "unverified sender" if rejected else "{}"
            return res

        with patch.object(config, "RESEND_API_KEY", "key"), patch.object(
            config, "BETA_SIGNUP_EMAIL_TO", "a@op.co,b@op.co"
        ), patch("requests.post", side_effect=respond) as post:
            res = self.client.post("/api/public/beta", json={"email": "x@y.co"})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(post.call_count, 2)
        with sqlite3.connect(self._db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM beta_signups").fetchone()[0]
        self.assertEqual(n, 1)


class StrategiesPayloadTests(PublicApiTestBase):
    def test_hq_aggregates(self) -> None:
        res = self.client.get("/api/public/strategies")
        self.assertEqual(res.status_code, 200)
        body = res.json()

        hq = body["hq"]["live"]
        # Only closed hq rows count: +20 and −10.
        self.assertEqual(hq["n_closed"], 2)
        self.assertEqual(hq["pnl_usd"], 10.0)
        self.assertEqual(hq["win_rate_pct"], 50.0)
        # Series starts at the sleeve and steps by daily realized P&L.
        sleeve = hq["sleeve_usd"]
        self.assertEqual(hq["series"][0], ["2026-09-01", sleeve])
        self.assertEqual(hq["series"][-1], ["2026-09-03", sleeve + 10.0])

        paper = body["hq"]["paper"]
        self.assertEqual(paper["n_closed"], 2)
        self.assertEqual(paper["tp1_reached"], 1)
        # Last equity mark of the day wins.
        self.assertEqual(paper["series"][-1], ["2026-08-07", 5050.0])

    def test_abstention_counts_hourly_only(self) -> None:
        body = self.client.get("/api/public/strategies").json()
        abstention = body["abstention"]
        # 4 hourly rows (3 no_trade + 1 spot_buy); the watchdog row is excluded.
        self.assertEqual(abstention["cycles"], 4)
        self.assertEqual(abstention["trade_actions"], 1)
        self.assertEqual(abstention["abstain_pct"], 75.0)

    def test_yield_from_snapshots_only(self) -> None:
        body = self.client.get("/api/public/strategies").json()
        y = body["yield"]
        self.assertEqual(y["n_days"], 2)
        self.assertEqual(y["nav_start_usd"], 4000.0)
        self.assertEqual(y["nav_usd"], 4040.0)
        self.assertEqual(y["pnl_pct"], 1.0)

    def test_optional_books_absent_without_dbs(self) -> None:
        # No IDEAS_DB / KALSHI_DB in the test env — sections simply absent.
        body = self.client.get("/api/public/strategies").json()
        self.assertNotIn("mill", body)
        self.assertNotIn("kalshi", body)

class IntelligenceTests(PublicApiTestBase):
    """The published market read behind the site's brain framing."""

    def _seed_intel(self) -> None:
        with sqlite3.connect(self._db) as conn:
            conn.executescript(
                """
                CREATE TABLE intel_long_thesis (
                    id INTEGER PRIMARY KEY, as_of_date TEXT,
                    cycle_phase TEXT, thesis_json TEXT, created_at TEXT
                );
                CREATE TABLE intel_stances (
                    id INTEGER PRIMARY KEY, cycle_ts TEXT, product_id TEXT,
                    timeframe TEXT, stance TEXT, confidence REAL,
                    source TEXT DEFAULT 'llm', created_at TEXT
                );
                CREATE TABLE macro_events (
                    id INTEGER PRIMARY KEY, severity INTEGER,
                    status TEXT, expires_at TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO intel_long_thesis"
                " (as_of_date, cycle_phase, thesis_json, created_at)"
                " VALUES ('2026-09-12', 'bull_expansion', ?, '2026-09-12T00:00:00Z')",
                ('{"bias": "bullish", "confidence": 0.6,'
                 ' "btc_thesis": "long prose that must not be published"}',),
            )
            # An older cycle, plus two runs inside the newest cycle_ts bucket
            # (the stance job fires twice per bucket in production).
            conn.executemany(
                "INSERT INTO intel_stances"
                " (cycle_ts, product_id, timeframe, stance, confidence, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("2026-09-12T09:00:00Z", "BTC-USD", "H1", "bearish", 0.9,
                     "2026-09-12T09:01:00Z"),
                    ("2026-09-12T10:00:00Z", "BTC-USD", "H4", "bullish", 0.5,
                     "2026-09-12T10:01:00Z"),
                    ("2026-09-12T10:00:00Z", "BTC-USD", "H1", "neutral", 0.4,
                     "2026-09-12T10:01:00Z"),
                    ("2026-09-12T10:00:00Z", "ETH-USD", "H4", "bullish", 0.3,
                     "2026-09-12T10:01:00Z"),
                    # Second run, same bucket: BTC H4 flipped bearish.
                    ("2026-09-12T10:00:00Z", "BTC-USD", "H4", "bearish", 0.77,
                     "2026-09-12T10:31:00Z"),
                ],
            )
            conn.executemany(
                "INSERT INTO macro_events (severity, status, expires_at)"
                " VALUES (?, ?, ?)",
                [
                    (5, "active", "2099-01-01T00:00:00Z"),
                    (3, "active", "2099-01-01T00:00:00Z"),
                    (4, "active", "2000-01-01T00:00:00Z"),  # expired
                    (0, "ignored", None),                   # never classified
                ],
            )
        public_api._cache_payload = None
        public_api._cache_at = 0.0

    def test_cycle_clock_is_deterministic(self) -> None:
        """No LLM and no network — the halving clock is arithmetic."""
        cycle = self.client.get("/api/public/strategies").json()[
            "intelligence"]["cycle"]
        self.assertEqual(cycle["halvings_tracked"], 4)
        self.assertEqual(cycle["last_halving"], "2024-04-20")
        self.assertEqual(cycle["next_halving_est"], "2028-04-15")
        self.assertGreater(cycle["days_since_halving"], 0)
        self.assertIn(cycle["phase"], (
            "post_halving_accumulation", "bull_expansion", "cycle_top_window",
            "bear_drawdown", "pre_halving_accumulation",
        ))
        self.assertTrue(0 <= cycle["progress_pct"] <= 100)

    def test_thesis_publishes_bias_but_not_prose(self) -> None:
        self._seed_intel()
        cycle = self.client.get("/api/public/strategies").json()[
            "intelligence"]["cycle"]
        self.assertEqual(cycle["thesis"]["bias"], "bullish")
        self.assertEqual(cycle["thesis"]["as_of"], "2026-09-12")
        # The operator's written thesis stays on the hub.
        self.assertNotIn("btc_thesis", cycle["thesis"])

    def test_stance_grid_is_the_newest_read_per_cell(self) -> None:
        """One row per product/timeframe, from the latest run in the bucket."""
        self._seed_intel()
        stances = self.client.get("/api/public/strategies").json()[
            "intelligence"]["stances"]
        self.assertEqual(stances["as_of"], "2026-09-12T10:31:00Z")
        self.assertEqual(
            sorted(stances["grid"]),
            [
                # BTC H4 shows the 10:31 flip, not the stale 10:01 read.
                ["BTC-USD", "H1", "neutral", 0.4],
                ["BTC-USD", "H4", "bearish", 0.77],
                ["ETH-USD", "H4", "bullish", 0.3],
            ],
        )

    def test_macro_counts_exclude_expired_and_ignored(self) -> None:
        self._seed_intel()
        macro = self.client.get("/api/public/strategies").json()[
            "intelligence"]["macro"]
        self.assertEqual(macro["headlines_seen"], 4)
        self.assertEqual(macro["classified"], 3)
        self.assertEqual(macro["active"], 2)   # the expired one drops out
        self.assertEqual(macro["max_severity"], 5)

    def test_missing_intel_tables_still_serve_the_cycle(self) -> None:
        """A fresh ledger has no brain tables; the books must still render."""
        body = self.client.get("/api/public/strategies").json()
        self.assertIn("cycle", body["intelligence"])
        self.assertNotIn("stances", body["intelligence"])
        self.assertIn("hq", body)


class MillEpochTests(PublicApiTestBase):
    """The mill book is re-based when the bracket changes.

    Prior ideas move to paper_trades_archive and the live table restarts, so
    the payload must keep the two epochs apart: reporting only the live table
    would erase the published record the day a bracket ships, and merging them
    into one hit rate would blend two different geometries.
    """

    def setUp(self) -> None:
        super().setUp()
        self._ideas = Path(self._tmpdir.name) / "ideas.db"
        with sqlite3.connect(self._ideas) as conn:
            conn.executescript(
                """
                CREATE TABLE paper_trades (
                    id INTEGER PRIMARY KEY, status TEXT,
                    pnl_pct REAL, opened_at TEXT
                );
                CREATE TABLE paper_trades_archive (
                    id INTEGER PRIMARY KEY, status TEXT,
                    pnl_pct REAL, opened_at TEXT
                );
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                """
            )
            # Prior bracket: 3 closed (1 winner) + 1 never resolved.
            conn.executemany(
                "INSERT INTO paper_trades_archive (status, pnl_pct, opened_at)"
                " VALUES (?, ?, ?)",
                [
                    ("hit_tp", 2.0, "2026-08-10T00:00:00Z"),
                    ("hit_sl", -1.0, "2026-08-10T06:00:00Z"),
                    ("hit_sl", -1.0, "2026-08-11T00:00:00Z"),
                    ("open", None, "2026-08-11T06:00:00Z"),
                ],
            )
            # Current bracket: one open idea, no closed record yet.
            conn.execute(
                "INSERT INTO paper_trades (status, pnl_pct, opened_at)"
                " VALUES ('open', NULL, '2026-08-12T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO meta (key, value)"
                " VALUES ('mill_paper_epoch_start', '2026-08-12T00:00:00Z')"
            )
        import trade_ideas_bridge

        self._mill_patch = patch.object(
            trade_ideas_bridge, "ideas_db_path", lambda: self._ideas
        )
        self._mill_patch.start()
        public_api._cache_payload = None
        public_api._cache_at = 0.0

    def tearDown(self) -> None:
        self._mill_patch.stop()
        super().tearDown()

    def test_epochs_reported_separately(self) -> None:
        mill = self.client.get("/api/public/strategies").json()["mill"]

        # Volume spans both epochs — the archive is not dropped.
        self.assertEqual(mill["n_ideas"], 5)
        self.assertEqual(mill["since"], "2026-08-10")
        self.assertEqual(mill["epoch_start"], "2026-08-12")

        prior = mill["prior_bracket"]
        self.assertEqual(prior["n_closed"], 3)
        self.assertEqual(prior["win_rate_pct"], round(1 / 3 * 100, 1))

        # The new bracket has no closed ideas, so it must claim no hit rate
        # rather than reporting 0% or borrowing the prior bracket's.
        current = mill["current_bracket"]
        self.assertEqual(current["n_ideas"], 1)
        self.assertEqual(current["n_closed"], 0)
        self.assertIsNone(current["win_rate_pct"])

    def test_missing_archive_table_is_not_fatal(self) -> None:
        """Before the first re-base there is no archive table at all."""
        with sqlite3.connect(self._ideas) as conn:
            conn.execute("DROP TABLE paper_trades_archive")
        public_api._cache_payload = None
        public_api._cache_at = 0.0

        mill = self.client.get("/api/public/strategies").json()["mill"]
        self.assertEqual(mill["n_ideas"], 1)
        self.assertEqual(mill["prior_bracket"]["n_ideas"], 0)
        self.assertIsNone(mill["prior_bracket"]["win_rate_pct"])


class CacheTests(PublicApiTestBase):
    def test_payload_is_cached(self) -> None:
        first = self.client.get("/api/public/strategies").json()
        with sqlite3.connect(self._db) as conn:
            conn.execute(
                "INSERT INTO live_trades"
                " (source, status, realized_pnl_usd, pnl_usd, opened_at, closed_at)"
                " VALUES ('hq','closed', 99.0, 99.0,"
                " '2026-09-05T00:00:00Z','2026-09-05T01:00:00Z')"
            )
        second = self.client.get("/api/public/strategies").json()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
