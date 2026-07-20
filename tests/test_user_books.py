"""Tests for personal demo accounts, offers, Accept/Reject, and /me tokens."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import paper
import user_books
from models import Suggestion


def _long_suggestion(**kwargs) -> Suggestion:
    base = dict(
        action="spot_buy",
        size=250.0,
        entry=2000.0,
        stop_loss=1900.0,
        take_profits=[2200.0, 2400.0],
        risk_reward=2.0,
        rationale="test long",
        product_id="ETH-USD",
    )
    base.update(kwargs)
    return Suggestion(**base)


class UserBooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_path = Path(self._tmpdir.name) / "test_ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", self._db_path),
            patch.object(config, "PAPER_PORTFOLIO_VALUE", 5000.0),
            patch.object(config, "ME_TOKEN_SECRET", "test-secret"),
            patch.object(config, "DASHBOARD_PUBLIC_URL", "https://dash.example"),
            patch.object(bot_config, "PAPER_ACCOUNT_SIZES", (500.0, 1000.0, 2500.0)),
            patch.object(bot_config, "PAPER_ACCOUNT_DEFAULT_USD", 1000.0),
            patch.object(bot_config, "APPROVAL_WINDOW_MIN", 15),
            patch.object(bot_config, "MISSED_CONNECTION_R", 0.5),
            patch.object(bot_config, "USER_MIN_DEPLOY_USD", 25.0),
            patch.object(bot_config, "TRADE_DEPLOY_PCT", 0.25),
            patch.object(bot_config, "HOUSE_CONTRIBUTION_TELEGRAM_ID", 0),
        ]
        for item in self._patches:
            item.start()
        paper.init_db()
        user_books.init_db()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        self._tmpdir.cleanup()

    def test_open_account_menu_once(self) -> None:
        first = user_books.open_paper_account(42, 500.0, "alice")
        self.assertTrue(first["ok"])
        self.assertEqual(first["cash_usd"], 500.0)
        second = user_books.open_paper_account(42, 1000.0, "alice")
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "already_opened")
        bad = user_books.open_paper_account(99, 123.0)
        self.assertEqual(bad["reason"], "invalid_amount")

    def test_migrate_funders(self) -> None:
        # Seed a legacy contribution row without going through fund_user house bump.
        import sqlite3

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO paper_contributions (telegram_id, amount_usd, created_at, username)
                VALUES (777, 1000, ?, 'legacy')
                """,
                (now,),
            )
            conn.commit()
        result = user_books.migrate_funders_to_personal_accounts()
        self.assertEqual(result["migrated"], 1)
        account = user_books.get_account(777)
        assert account is not None
        self.assertEqual(account["starting_usd"], 1000.0)
        again = user_books.migrate_funders_to_personal_accounts()
        self.assertEqual(again["migrated"], 0)
        self.assertEqual(again["skipped"], 1)

    def test_accept_reject_and_metrics(self) -> None:
        user_books.open_paper_account(1, 1000.0, "u1")
        suggestion = _long_suggestion()
        offer = user_books.create_trade_offer(
            cycle_id="cycleA_ETH",
            suggestion=suggestion,
            chart_paths=["charts/decision.png", "charts/structure.png"],
            house_position_id=None,
        )
        assert offer is not None
        rejected = user_books.reject_offer(offer["offer_id"], 1)
        self.assertTrue(rejected["ok"])

        user_books.open_paper_account(2, 1000.0, "u2")
        # Re-seed pending for user 2 on same offer
        import sqlite3

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO trade_decisions
                    (offer_id, telegram_id, status, decided_at)
                VALUES (?, 2, 'pending', NULL)
                """,
                (offer["offer_id"],),
            )
            conn.commit()

        accepted = user_books.accept_offer(offer["offer_id"], 2)
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["status"], "accepted")
        metrics = user_books.get_user_metrics(2, spots={"ETH-USD": 2000.0})
        self.assertTrue(metrics["ok"])
        self.assertLess(metrics["cash_usd"], 1000.0)
        self.assertEqual(metrics["open_count"], 1)

    def test_expire_pending(self) -> None:
        user_books.open_paper_account(3, 1000.0)
        suggestion = _long_suggestion()
        past = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        offer = user_books.create_trade_offer(
            cycle_id="cycleB_ETH",
            suggestion=suggestion,
            chart_paths=[],
            expires_at=past,
        )
        assert offer is not None
        n = user_books.expire_pending_decisions()
        self.assertGreaterEqual(n, 1)
        decision = user_books.get_decision(offer["offer_id"], 3)
        assert decision is not None
        self.assertEqual(decision["status"], "expired")

    def test_me_token_roundtrip(self) -> None:
        token = user_books.create_me_token(55, ttl_sec=60)
        self.assertEqual(user_books.verify_me_token(token), 55)
        self.assertIsNone(user_books.verify_me_token("bad.token.here"))
        url = user_books.me_url(55)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertTrue(url.startswith("https://dash.example/me?t="))

    def test_missed_connection_idempotent_flag(self) -> None:
        user_books.open_paper_account(8, 1000.0)
        suggestion = _long_suggestion()
        offer = user_books.create_trade_offer(
            cycle_id="cycleC_ETH",
            suggestion=suggestion,
            chart_paths=["x_decision.png"],
        )
        assert offer is not None
        user_books.reject_offer(offer["offer_id"], 8)
        user_books.mark_missed_connection_sent(offer["offer_id"])
        refreshed = user_books.get_offer(offer["offer_id"])
        assert refreshed is not None
        self.assertEqual(int(refreshed["missed_connection_sent"]), 1)

    def test_prospective_risk_reward(self) -> None:
        rr = user_books.prospective_risk_reward_usd(
            entry=100.0,
            stop_loss=90.0,
            take_profit=120.0,
            side="long",
            notional_usd=1000.0,
        )
        self.assertAlmostEqual(rr["risk_usd"], 100.0)
        self.assertAlmostEqual(rr["reward_usd"], 200.0)

    def test_get_user_metrics_via_paper(self) -> None:
        user_books.open_paper_account(11, 2500.0)
        metrics = paper.get_user_metrics(11, spots={"ETH-USD": 3000.0})
        self.assertTrue(metrics["ok"])
        self.assertEqual(metrics["amount_usd"], 2500.0)
        self.assertAlmostEqual(metrics["equity_usd"], 2500.0)


class DecisionChartTests(unittest.TestCase):
    def test_build_decision_chart_long(self) -> None:
        import charts

        bars = []
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        price = 2000.0
        for i in range(40):
            ts = (base + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            bars.append(
                {
                    "ts": ts,
                    "open": price,
                    "high": price + 10,
                    "low": price - 10,
                    "close": price + 1,
                    "volume": 1.0,
                }
            )
            price += 1
        suggestion = _long_suggestion()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            charts_dir = Path(tmp)
            with patch.object(config, "CHARTS_DIR", charts_dir):
                path = charts.build_decision_chart(
                    suggestion, {"M5": bars}, "testcycle_ETH"
                )
            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(Path(path).exists())
            self.assertIn("decision", path)


if __name__ == "__main__":
    unittest.main()
