"""Kalshi autopilot: subscribing users ride every lane trade — their own
positions only, never a shared book.

The fiduciary properties pinned here:
- the flag is per (user, strategy) and implies a subscription, never funding;
- the sweep enters exactly the opted-in users, each sized from their own
  allocation through the same reserve-first path as a manual Accept;
- one entry per (window, user), no matter how many sweeps or restarts — the
  in-memory guard and the pool's unique intent_ref both refuse a repeat;
- a user with autopilot on and no allocation stays flat with nothing reserved.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import pool

ADMIN = 111
ALICE = 1001
BOB = 1002


class KalshiAutopilotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_MIN_EQUITY_USD", 10.0),
            patch.object(bot_config, "POOL_DEPLOY_FEE_USD", 0.0),
            patch.object(bot_config, "POOL_KALSHI_RISK_PCT", 0.05),
            patch.object(bot_config, "KALSHI_MAX_CONTRACTS_PER_ACCEPT", 100),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        pool.init_db()

        import kalshi_execute

        kalshi_execute._autopilot_seen.clear()
        kalshi_execute._autopilot_retry_at.clear()
        self.addCleanup(kalshi_execute._autopilot_seen.clear)
        self.addCleanup(kalshi_execute._autopilot_retry_at.clear)

    def _fund_and_deploy(self, uid: int, alloc: float = 200.0) -> None:
        pool.approve_user(uid, admin_id=ADMIN)
        self.assertTrue(pool.credit(uid, 1000.0, admin_id=ADMIN)["ok"])
        self.assertTrue(pool.set_allocation(uid, "kalshi_wick", alloc)["ok"])

    def _sweep_mocks(self, stack: ExitStack, *, ask: int = 71) -> None:
        """Gateway and ledger mocks for one clean fill at `ask` cents."""
        import kalshi_execute
        import kalshi_gateway

        stack.enter_context(
            patch.object(kalshi_execute, "enabled", return_value=True))
        stack.enter_context(
            patch.object(kalshi_execute, "fresh_entries",
                         side_effect=lambda s: [42] if s == "kalshi_wick" else []))
        stack.enter_context(
            patch.object(kalshi_execute, "resolve_card", return_value={
                "ok": True, "market_ticker": "KXBTC-TEST", "side": "yes",
                "entry_cents": 70.0, "position_id": 42, "age_min": 0.5}))
        stack.enter_context(
            patch.object(kalshi_gateway, "get_market",
                         return_value={"status": "active", "yes_ask": ask}))
        stack.enter_context(
            patch.object(kalshi_gateway, "place_limit_buy",
                         return_value={"order_id": "ord-1"}))
        stack.enter_context(
            patch.object(kalshi_gateway, "get_order",
                         return_value={"remaining_count": 0,
                                       "status": "executed"}))
        stack.enter_context(
            patch.object(kalshi_gateway, "fill_summary",
                         return_value={"contracts": 13, "avg_cents": 71.0,
                                       "cost_usd": 9.38, "fee_usd": 0.15}))


class AutopilotFlagTests(KalshiAutopilotTestCase):
    def test_enabling_implies_subscription(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        self.assertNotIn("kalshi_wick", pool.strategy_subscriptions(ALICE))
        pool.set_autopilot(ALICE, "kalshi_wick", True)
        self.assertIn("kalshi_wick", pool.strategy_subscriptions(ALICE))
        self.assertTrue(pool.autopilot_enabled(ALICE, "kalshi_wick"))

    def test_toggle_off_keeps_the_subscription(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        pool.set_autopilot(ALICE, "kalshi_wick", True)
        pool.set_autopilot(ALICE, "kalshi_wick", False)
        self.assertFalse(pool.autopilot_enabled(ALICE, "kalshi_wick"))
        self.assertIn("kalshi_wick", pool.strategy_subscriptions(ALICE))

    def test_user_ids_require_approval_and_the_flag(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        pool.set_autopilot(ALICE, "kalshi_wick", True)
        # BOB flips the flag but was never approved — the sweep must not
        # see him, approval is the money gate everywhere else too.
        pool.set_autopilot(BOB, "kalshi_wick", True)
        pool.subscribe_strategy(ALICE, "kalshi_reversal")  # sub ≠ autopilot
        self.assertEqual(pool.autopilot_user_ids("kalshi_wick"), [ALICE])
        self.assertEqual(pool.autopilot_user_ids("kalshi_reversal"), [])


class AutopilotSweepTests(KalshiAutopilotTestCase):
    def test_sweep_enters_only_opted_in_users(self) -> None:
        import kalshi_execute

        self._fund_and_deploy(ALICE)
        self._fund_and_deploy(BOB)
        pool.set_autopilot(ALICE, "kalshi_wick", True)
        pool.subscribe_strategy(BOB, "kalshi_wick")  # subscribed, no autopilot

        with ExitStack() as stack:
            self._sweep_mocks(stack)
            results = kalshi_execute.autopilot_sweep()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"], results[0])
        self.assertEqual(results[0]["telegram_id"], ALICE)
        rows = pool.open_kalshi_rows("open")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["telegram_id"]), ALICE)
        self.assertEqual(rows[0]["intent_ref"], f"k_kalshi_wick:42:{ALICE}")
        # BOB's money never moved.
        self.assertAlmostEqual(
            float(pool.get_account(BOB)["reserved_usd"]), 0.0, places=2)

    def test_sweep_is_idempotent_even_across_a_restart(self) -> None:
        import kalshi_execute

        self._fund_and_deploy(ALICE)
        pool.set_autopilot(ALICE, "kalshi_wick", True)

        with ExitStack() as stack:
            self._sweep_mocks(stack)
            first = kalshi_execute.autopilot_sweep()
            second = kalshi_execute.autopilot_sweep()
            # Simulate a restart: the in-memory guard is gone, so only the
            # pool's unique intent_ref stands between the user and a double
            # position.
            kalshi_execute._autopilot_seen.clear()
            third = kalshi_execute.autopilot_sweep()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(third, [])  # already_recorded is filtered, not news
        self.assertEqual(len(pool.open_kalshi_rows("open")), 1)
        self.assertAlmostEqual(
            float(pool.get_account(ALICE)["reserved_usd"]), 9.38, places=2)

    def test_no_allocation_stays_flat_and_reports_why(self) -> None:
        import kalshi_execute

        pool.approve_user(ALICE, admin_id=ADMIN)
        self.assertTrue(pool.credit(ALICE, 1000.0, admin_id=ADMIN)["ok"])
        pool.set_autopilot(ALICE, "kalshi_wick", True)  # on, nothing deployed

        with ExitStack() as stack:
            self._sweep_mocks(stack)
            results = kalshi_execute.autopilot_sweep()

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["reason"], "no_allocation")
        self.assertEqual(pool.open_kalshi_rows("open"), [])
        self.assertAlmostEqual(
            float(pool.get_account(ALICE)["reserved_usd"]), 0.0, places=2)

    def test_manual_accept_after_autopilot_is_refused(self) -> None:
        import kalshi_execute

        self._fund_and_deploy(ALICE)
        pool.set_autopilot(ALICE, "kalshi_wick", True)

        with ExitStack() as stack:
            self._sweep_mocks(stack)
            kalshi_execute.autopilot_sweep()
            manual = kalshi_execute.accept(ALICE, "kalshi_wick", "42")

        self.assertFalse(manual["ok"])
        self.assertEqual(manual["reason"], "already_recorded")
        self.assertEqual(len(pool.open_kalshi_rows("open")), 1)

    def test_transient_quote_failure_retries_after_backoff(self) -> None:
        import kalshi_execute
        import kalshi_gateway

        self._fund_and_deploy(ALICE)
        pool.set_autopilot(ALICE, "kalshi_wick", True)

        with ExitStack() as stack:
            self._sweep_mocks(stack)
            stack.enter_context(
                patch.object(kalshi_gateway, "get_market",
                             side_effect=kalshi_gateway.KalshiError("down")))
            flaky = kalshi_execute.autopilot_sweep()
            # The sweep now runs every few seconds — inside the backoff
            # window the same refusal must not re-quote the venue.
            held = kalshi_execute.autopilot_sweep()
        self.assertEqual(flaky[0]["reason"], "quote_failed")
        self.assertEqual(held, [])

        # Venue back up and the backoff expired, same process lifetime.
        kalshi_execute._autopilot_retry_at[(42, ALICE)] = 0.0
        with ExitStack() as stack:
            self._sweep_mocks(stack)
            retried = kalshi_execute.autopilot_sweep()
        self.assertEqual(len(retried), 1)
        self.assertTrue(retried[0]["ok"], retried[0])
        self.assertEqual(len(pool.open_kalshi_rows("open")), 1)


class AutopilotAttemptJournalTests(KalshiAutopilotTestCase):
    """Every attempt lands in pool_kalshi_attempts — the slip gate's only
    future tuning data, since the order book is recorded nowhere else."""

    def test_a_fill_is_journaled_with_quote_and_lag(self) -> None:
        import kalshi_execute

        self._fund_and_deploy(ALICE)
        pool.set_autopilot(ALICE, "kalshi_wick", True)

        with ExitStack() as stack:
            self._sweep_mocks(stack)
            kalshi_execute.autopilot_sweep()

        rows = pool.kalshi_attempt_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["outcome"], "filled")
        self.assertEqual(int(row["position_id"]), 42)
        self.assertEqual(row["market_ticker"], "KXBTC-TEST")
        self.assertAlmostEqual(float(row["entry_cents"]), 70.0)
        self.assertAlmostEqual(float(row["ask_cents"]), 71.0)
        self.assertAlmostEqual(float(row["lag_sec"]), 30.0)

    def test_a_slipped_refusal_journals_the_gap(self) -> None:
        import kalshi_execute

        self._fund_and_deploy(ALICE)
        pool.set_autopilot(ALICE, "kalshi_wick", True)

        with ExitStack() as stack:
            self._sweep_mocks(stack, ask=80)  # 10¢ over a 3¢ gate
            results = kalshi_execute.autopilot_sweep()

        self.assertEqual(results[0]["reason"], "slipped")
        rows = pool.kalshi_attempt_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "slipped")
        self.assertAlmostEqual(float(rows[0]["entry_cents"]), 70.0)
        self.assertAlmostEqual(float(rows[0]["ask_cents"]), 80.0)
        # Refused before any reserve — nothing opened, nothing held.
        self.assertEqual(pool.open_kalshi_rows("open"), [])
        self.assertAlmostEqual(
            float(pool.get_account(ALICE)["reserved_usd"]), 0.0, places=2)

    def test_journal_failure_never_blocks_the_entry(self) -> None:
        import kalshi_execute

        self._fund_and_deploy(ALICE)
        pool.set_autopilot(ALICE, "kalshi_wick", True)

        with ExitStack() as stack:
            self._sweep_mocks(stack)
            stack.enter_context(
                patch.object(pool, "record_kalshi_attempt",
                             side_effect=RuntimeError("journal down")))
            results = kalshi_execute.autopilot_sweep()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"], results[0])
        self.assertEqual(len(pool.open_kalshi_rows("open")), 1)


if __name__ == "__main__":
    unittest.main()
