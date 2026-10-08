"""Autopilot "what did I miss?" counterfactual: recorded replay, labeled estimate.

Pinned properties:
- the replay sizes each house window with the live Accept formula
  (alloc × POOL_KALSHI_RISK_PCT, whole contracts at price + taker fee) and
  settles wins/losses/voids the way settle_sweep books them;
- house trades from before the user's first deposit are excluded — the
  window is "since my funds arrived", not the strategy's inception;
- nothing is computed without a deployment, a deposit, and the house ledger;
- every rendered figure carries the estimate caveat.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import counterfactual
import pool
import telegram_ui

ADMIN = 111
ALICE = 1001


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class CounterfactualTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.house_db = tmp / "kalshi.db"
        patches = [
            patch.object(config, "LEDGER_DB", tmp / "ledger.db"),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_MIN_EQUITY_USD", 10.0),
            patch.object(bot_config, "POOL_DEPLOY_FEE_USD", 0.0),
            patch.object(bot_config, "POOL_KALSHI_RISK_PCT", 0.05),
            patch.object(bot_config, "KALSHI_MAX_CONTRACTS_PER_ACCEPT", 100),
            patch.dict(os.environ, {"KALSHI_DB": str(self.house_db)}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        pool.init_db()

    def _fund_and_deploy(self, uid: int, alloc: float = 200.0) -> None:
        pool.approve_user(uid, admin_id=ADMIN)
        self.assertTrue(pool.credit(uid, 1000.0, admin_id=ADMIN)["ok"])
        self.assertTrue(pool.set_allocation(uid, "kalshi_wick", alloc)["ok"])

    def _write_house_trades(self, rows: list[tuple[str, str, str, float, str]]) -> None:
        """rows: (bot_id, side, result, entry_cents, opened_at)."""
        conn = sqlite3.connect(self.house_db)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS paper_positions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, side TEXT,"
            " result TEXT, entry_cents REAL, opened_at TEXT, status TEXT)"
        )
        conn.executemany(
            "INSERT INTO paper_positions"
            " (bot_id, side, result, entry_cents, opened_at, status)"
            " VALUES (?, ?, ?, ?, ?, 'settled')",
            rows,
        )
        conn.commit()
        conn.close()

    def _settle_user_trade(self, uid: int, pnl: float) -> None:
        with pool._connect() as conn:
            conn.execute(
                "INSERT INTO pool_kalshi_positions"
                " (telegram_id, strategy, market_ticker, side, contracts,"
                "  entry_cents, cost_usd, fee_usd, intent_ref, status, result,"
                "  pnl_usd, created_at, settled_at)"
                " VALUES (?, 'kalshi_wick', 'KXTEST', 'yes', 10, 70, 7.0, 0.2,"
                f" 'test:{pnl}', 'settled', 'yes', ?, ?, ?)",
                (uid, pnl, pool._now(), pool._now()),
            )


class WhatIfMathTests(CounterfactualTestCase):
    def test_replays_house_book_with_live_sizing_and_fees(self) -> None:
        """$200 deployed at 5% = $10/window → 13 contracts at 70¢+2¢ fee.
        Win +$3.70, loss −$9.30, void flat; pre-deposit trades excluded."""
        self._fund_and_deploy(ALICE, alloc=200.0)
        self._settle_user_trade(ALICE, 2.50)
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        # Sides are uppercase and results lowercase, matching the real
        # bots' ledger — the 10-08 live run showed a cased comparison
        # counting all 180 recorded wins as losses.
        self._write_house_trades([
            # Before the deposit — must not count.
            ("eva_wick", "YES", "yes", 70.0, "2000-01-01T00:00:00Z"),
            ("eva_wick", "YES", "yes", 70.0, _iso(later)),
            ("eva_wick", "YES", "no", 70.0, _iso(later + timedelta(minutes=15))),
            ("eva_wick", "NO", "", 50.0, _iso(later + timedelta(minutes=30))),
            # Another bot's book — must not count.
            ("eva_streak", "YES", "yes", 70.0, _iso(later)),
        ])

        d = counterfactual.autopilot_what_if(ALICE)

        self.assertTrue(d["ok"])
        self.assertEqual(d["house_trades"], 3)
        self.assertEqual(d["sized_trades"], 3)
        self.assertEqual((d["wins"], d["losses"], d["voids"]), (1, 1, 1))
        # taker_fee_usd(70, 1) = $0.02 → per-contract $0.72 → 13 contracts.
        # Win: 13 × $0.30 − $0.20 fee = +$3.70; loss: −13 × $0.70 − $0.20 = −$9.30.
        self.assertAlmostEqual(d["est_pnl_usd"], -5.60, places=2)
        self.assertAlmostEqual(d["est_fees_usd"], 0.40, places=2)
        self.assertEqual(d["actual_trades"], 1)
        self.assertAlmostEqual(d["actual_pnl_usd"], 2.50, places=2)
        self.assertAlmostEqual(d["gap_usd"], -8.10, places=2)
        self.assertAlmostEqual(d["alloc_usd"], 200.0, places=2)

    def test_allocation_too_small_for_one_contract_skips(self) -> None:
        """A $10 deployment budgets $0.50/window — refused, never stretched."""
        self._fund_and_deploy(ALICE, alloc=50.0)
        with patch.object(bot_config, "POOL_MIN_DEPLOY_USD", 1.0):
            pool.set_allocation(ALICE, "kalshi_wick", 10.0)
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        self._write_house_trades([("eva_wick", "yes", "yes", 70.0, _iso(later))])
        d = counterfactual.autopilot_what_if(ALICE)
        self.assertTrue(d["ok"])
        self.assertEqual(d["sized_trades"], 0)
        self.assertEqual(d["skipped_too_small"], 1)
        self.assertEqual(d["est_pnl_usd"], 0.0)


class WhatIfGateTests(CounterfactualTestCase):
    def test_refuses_without_an_allocation(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        pool.credit(ALICE, 1000.0, admin_id=ADMIN)
        d = counterfactual.autopilot_what_if(ALICE)
        self.assertFalse(d["ok"])
        self.assertEqual(d["reason"], "no_allocation")

    def test_refuses_without_the_house_ledger(self) -> None:
        self._fund_and_deploy(ALICE)
        # KALSHI_DB points at a path that does not exist.
        d = counterfactual.autopilot_what_if(ALICE)
        self.assertFalse(d["ok"])
        self.assertEqual(d["reason"], "ledger_unavailable")


class WhatIfRenderTests(CounterfactualTestCase):
    def test_portfolio_lines_carry_the_estimate_caveat(self) -> None:
        self._fund_and_deploy(ALICE, alloc=200.0)
        self._settle_user_trade(ALICE, 2.50)
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        self._write_house_trades([("eva_wick", "yes", "yes", 70.0, _iso(later))])

        d = counterfactual.autopilot_what_if(ALICE)
        lines = telegram_ui.format_autopilot_what_if(d)
        text = "\n".join(lines)
        self.assertIn("What if — autopilot", text)
        self.assertIn("$+2.50", text)      # actual, recorded
        self.assertIn("$+3.70", text)      # estimate
        self.assertIn("Estimate", text)    # caveat always rendered
        self.assertIn("slip", text)

        p = pool.portfolio(ALICE)
        p["autopilot_what_if"] = d
        rendered = telegram_ui.format_portfolio(p)
        self.assertIn("What if — autopilot", rendered)

    def test_nothing_rendered_when_not_ok_or_empty(self) -> None:
        self.assertEqual(
            telegram_ui.format_autopilot_what_if({"ok": False, "reason": "x"}), []
        )
        self.assertEqual(telegram_ui.format_autopilot_what_if({}), [])


if __name__ == "__main__":
    unittest.main()
