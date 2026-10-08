"""Phase 1: test-wallet deposits, Kalshi lane accounting, treasury journal.

The properties pinned here are fiduciary, same standard as test_pool:
attribution is by sender and never by guess, every balance move is a journal
row that survives re-runs (idempotent by ref), a Kalshi position's reserve
always equals what the user genuinely has committed at the venue, and the
reconcile total refuses to exist when any configured leg cannot be read.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import pool
import treasury

ADMIN = 111
ALICE = 1001
BOB = 1002

COINBASE_ADDRESS = "0xDdA10FB6e6d726ae1cfB079CD79A4f0Ef7cAF240"
TEST_WALLET = "0x00000000000000000000000000000000000dead0"


def txhash(seed: str) -> str:
    return "0x" + (seed * 64)[:64]


def wallet_for(uid: int) -> str:
    return "0x" + f"{uid:040x}"


class Phase1TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "POOL_DEPOSIT_ADDRESS", COINBASE_ADDRESS),
            patch.object(config, "TEST_WALLET_ADDRESS", TEST_WALLET),
            patch.object(config, "TEST_WALLET_CHAIN_ID", 8453),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_MIN_EQUITY_USD", 10.0),
            patch.object(bot_config, "POOL_MIN_DEPOSIT_USD", 20.0),
            patch.object(bot_config, "POOL_MIN_DEPLOY_USD", 5.0),
            # Existing cases size allocations to the full cash claim; the
            # live $5 deploy fee is covered by dedicated DeployFeeTests.
            patch.object(bot_config, "POOL_DEPLOY_FEE_USD", 0.0),
            patch.object(bot_config, "POOL_KALSHI_RISK_PCT", 0.05),
            patch.object(bot_config, "KALSHI_MAX_CONTRACTS_PER_ACCEPT", 100),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        pool.init_db()

    def _fund(self, uid: int, amount: float) -> None:
        pool.approve_user(uid, admin_id=ADMIN)
        result = pool.credit(uid, amount, admin_id=ADMIN)
        self.assertTrue(result["ok"], result)

    def _transfer(self, *, txid: str, sender: str, amount: float) -> dict:
        return {"txid": txid, "from": sender, "amount_usd": amount,
                "confirmations": 30}


# ---------------------------------------------------------------------------
# Test-wallet deposits
# ---------------------------------------------------------------------------

class TestWalletDepositTests(Phase1TestCase):
    def setUp(self) -> None:
        super().setUp()
        # Baseline pass over an empty history so subsequent sweeps are live.
        pool.observe_testwallet_deposits([])

    def test_registered_sender_credits_and_verifies_wallet(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        address = wallet_for(ALICE)
        pool.register_wallet(ALICE, address)

        events = pool.observe_testwallet_deposits(
            [self._transfer(txid=txhash("a"), sender=address, amount=250.0)]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "credited")
        self.assertEqual(events[0]["telegram_id"], ALICE)
        account = pool.get_account(ALICE)
        self.assertEqual(float(account["cash_usd"]), 250.0)
        wallet = pool.get_wallet(ALICE)
        self.assertEqual(wallet["status"], "verified")

    def test_rerun_is_a_noop(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        address = wallet_for(ALICE)
        pool.register_wallet(ALICE, address)
        batch = [self._transfer(txid=txhash("b"), sender=address, amount=100.0)]

        pool.observe_testwallet_deposits(batch)
        events = pool.observe_testwallet_deposits(batch)
        self.assertEqual(events, [])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 100.0)

    def test_unknown_sender_is_held_not_apportioned(self) -> None:
        self._fund(ALICE, 500.0)  # a funded account exists — no excuse to guess
        events = pool.observe_testwallet_deposits(
            [self._transfer(txid=txhash("c"), sender=wallet_for(9999),
                            amount=300.0)]
        )
        self.assertEqual(events[0]["kind"], "unmatched")
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 500.0)
        self.assertEqual(len(pool.unmatched_testwallet_deposits()), 1)

    def test_assign_credits_exactly_once(self) -> None:
        pool.approve_user(BOB, admin_id=ADMIN)
        tx = txhash("d")
        pool.observe_testwallet_deposits(
            [self._transfer(txid=tx, sender=wallet_for(9999), amount=75.0)]
        )
        first = pool.assign_testwallet_deposit(tx, BOB, admin_id=ADMIN)
        self.assertTrue(first["ok"], first)
        self.assertEqual(float(pool.get_account(BOB)["cash_usd"]), 75.0)
        second = pool.assign_testwallet_deposit(tx, BOB, admin_id=ADMIN)
        self.assertFalse(second["ok"])
        self.assertEqual(float(pool.get_account(BOB)["cash_usd"]), 75.0)

    def test_below_minimum_is_held(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        address = wallet_for(ALICE)
        pool.register_wallet(ALICE, address)
        events = pool.observe_testwallet_deposits(
            [self._transfer(txid=txhash("e"), sender=address, amount=5.0)]
        )
        self.assertEqual(events[0]["kind"], "unmatched")
        self.assertIn("minimum", events[0]["reason"])

    def test_first_run_baselines_unknown_history_but_credits_registered(self) -> None:
        pool.set_meta("_reset", "x")  # ensure meta table exists
        pool.del_meta(pool._TESTWALLET_BASELINE_KEY)  # noqa: SLF001 — test resets the first-run flag
        pool.approve_user(ALICE, admin_id=ADMIN)
        address = wallet_for(ALICE)
        pool.register_wallet(ALICE, address)

        events = pool.observe_testwallet_deposits([
            self._transfer(txid=txhash("f"), sender=wallet_for(9999),
                           amount=1000.0),
            self._transfer(txid=txhash("9"), sender=address, amount=200.0),
        ])
        kinds = {e["kind"] for e in events}
        self.assertEqual(kinds, {"credited"})
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 200.0)
        # The unknown historic transfer is baseline, not an unmatched alert.
        self.assertEqual(pool.unmatched_testwallet_deposits(), [])

    # -- the same EOA on Base and Ethereum ---------------------------------

    def test_each_chain_baselines_separately_and_records_where_money_landed(self) -> None:
        """Base has been watched for weeks; Ethereum is switched on today.
        The pre-existing Ethereum history is house history, not a stranger's
        unclaimed money — and a registered sender credits on either chain."""
        pool.approve_user(ALICE, admin_id=ADMIN)
        address = wallet_for(ALICE)
        pool.register_wallet(ALICE, address)
        # Base already baselined (setUp); Ethereum never seen before.
        pool.observe_testwallet_deposits([], chain_id=8453)
        events = pool.observe_testwallet_deposits([
            self._transfer(txid=txhash("1"), sender=wallet_for(9999), amount=5000.0),
            self._transfer(txid=txhash("2"), sender=address, amount=150.0),
        ], chain_id=1)
        self.assertEqual([e["kind"] for e in events], ["credited"])
        self.assertEqual(events[0]["chain_id"], 1)
        self.assertEqual(pool.unmatched_testwallet_deposits(), [])
        # A later Base arrival from an unknown sender is live, not baseline.
        events = pool.observe_testwallet_deposits([
            self._transfer(txid=txhash("3"), sender=wallet_for(9999), amount=40.0),
        ], chain_id=8453)
        self.assertEqual(events[0]["kind"], "unmatched")
        held = pool.unmatched_testwallet_deposits()
        self.assertEqual([(h["txid"], h["chain_id"]) for h in held],
                         [(txhash("3"), 8453)])

    def test_assign_reports_whether_the_sender_is_the_registered_wallet(self) -> None:
        """An exchange withdrawal can be credited by hand, but it proves
        nothing about the user's wallet — the result says so, so the bot can
        warn both sides that withdrawals stay blocked."""
        pool.approve_user(BOB, admin_id=ADMIN)
        pool.register_wallet(BOB, wallet_for(BOB))
        hot_wallet = wallet_for(424242)
        tx = txhash("e")
        pool.observe_testwallet_deposits(
            [self._transfer(txid=tx, sender=hot_wallet, amount=90.0)]
        )
        result = pool.assign_testwallet_deposit(tx, BOB, admin_id=ADMIN)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sender"], hot_wallet)
        self.assertFalse(result["sender_is_registered"])
        self.assertEqual(pool.get_wallet(BOB)["status"], "pending")
        # ...and the withdrawal path agrees: unproven wallet, no payout.
        self.assertFalse(pool.request_withdrawal(BOB, 50.0)["ok"])

    def test_sweep_reads_every_chain_and_one_failing_source_does_not_hide_the_other(self) -> None:
        import chain
        import watchdog

        pool.approve_user(ALICE, admin_id=ADMIN)
        address = wallet_for(ALICE)
        pool.register_wallet(ALICE, address)
        for cid in (8453, 1):
            pool.observe_testwallet_deposits([], chain_id=cid)

        def fake_recent(addr, *, chain_id, cursor_block=None, limit=50):
            if chain_id == 8453:
                raise chain.ChainError("rpc down")
            return ([{"txid": txhash("7"), "from": address, "to": addr,
                      "amount_usd": 120.0, "confirmations": 40,
                      "block": 100, "timestamp": 0, "symbol": "USDC"}],
                    None)

        sent: list[tuple[int, str]] = []
        with patch.object(config, "TEST_WALLET_CHAIN_IDS", (8453, 1)), \
             patch.object(chain, "readable", return_value=True), \
             patch.object(chain, "recent_inbound_usdc", side_effect=fake_recent), \
             patch("notify.send_pool_dm", side_effect=lambda uid, text: sent.append((uid, text))), \
             patch("notify.send_pool_admin_alert", lambda text: None):
            watchdog._testwallet_deposit_sweep()  # noqa: SLF001
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 120.0)
        self.assertEqual(pool.get_wallet(ALICE)["status"], "verified")
        self.assertEqual(len(sent), 1)
        self.assertIn("on Ethereum", sent[0][1])

    def test_sweep_persists_an_rpc_cursor_per_chain(self) -> None:
        import chain
        import watchdog

        pool.observe_testwallet_deposits([], chain_id=8453)
        pool.set_meta("testwallet_scan_block:8453", "1000")
        seen: list[int | None] = []

        def fake_recent(addr, *, chain_id, cursor_block=None, limit=50):
            seen.append(cursor_block)
            return [], 5000

        with patch.object(config, "TEST_WALLET_CHAIN_IDS", (8453,)), \
             patch.object(chain, "readable", return_value=True), \
             patch.object(chain, "recent_inbound_usdc", side_effect=fake_recent), \
             patch("notify.send_pool_admin_alert", lambda text: None):
            watchdog._testwallet_deposit_sweep()  # noqa: SLF001
        self.assertEqual(seen, [1000])
        self.assertEqual(pool.get_meta("testwallet_scan_block:8453"), "5000")


# ---------------------------------------------------------------------------
# Kalshi lane accounting
# ---------------------------------------------------------------------------

class KalshiAccountingTests(Phase1TestCase):
    def _deploy(self, uid: int, cash: float, alloc: float) -> None:
        self._fund(uid, cash)
        result = pool.set_allocation(uid, "kalshi_wick", alloc)
        self.assertTrue(result.get("ok"), result)

    def test_sizing_reserve_trim_and_win(self) -> None:
        self._deploy(ALICE, 1000.0, 200.0)
        # budget = min(200, 1000) × 5% = $10; per contract at 70¢ + $0.02 fee
        # = $0.72 → 13 contracts, max cost $9.36.
        placed = pool.open_kalshi_placing(
            "k_test:1:1001", ALICE, "kalshi_wick",
            market_ticker="KXBTC-TEST", side="yes", limit_cents=70,
            fee_per_contract_usd=0.02,
        )
        self.assertTrue(placed["ok"], placed)
        self.assertEqual(placed["contracts"], 13)
        self.assertAlmostEqual(placed["max_cost_usd"], 9.36, places=2)
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(float(account["reserved_usd"]), 9.36, places=2)

        # Venue filled 10 of 13 — reserve trims to the actual cost.
        booked = pool.finish_kalshi_open(
            "k_test:1:1001", filled_contracts=10, entry_cents=70.0,
            cost_usd=7.15, fee_usd=0.15, order_id="ord-1",
        )
        self.assertTrue(booked["ok"], booked)
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(float(account["reserved_usd"]), 7.15, places=2)
        self.assertAlmostEqual(float(account["cash_usd"]), 1000.0, places=2)

        # Settles a winner: 10 × $1 payout.
        row = pool.open_kalshi_rows("open")[0]
        settled = pool.settle_kalshi_position(
            int(row["id"]), result="yes", payout_usd=10.0,
        )
        self.assertTrue(settled["ok"], settled)
        self.assertAlmostEqual(settled["pnl_usd"], 2.85, places=2)
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(float(account["reserved_usd"]), 0.0, places=2)
        self.assertAlmostEqual(float(account["cash_usd"]), 1002.85, places=2)

    def test_settle_is_idempotent(self) -> None:
        self._deploy(ALICE, 1000.0, 200.0)
        pool.open_kalshi_placing(
            "k_test:2:1001", ALICE, "kalshi_wick",
            market_ticker="KXBTC-TEST", side="no", limit_cents=50,
            fee_per_contract_usd=0.02,
        )
        pool.finish_kalshi_open(
            "k_test:2:1001", filled_contracts=5, entry_cents=50.0,
            cost_usd=2.59, fee_usd=0.09, order_id="ord-2",
        )
        row = pool.open_kalshi_rows("open")[0]
        first = pool.settle_kalshi_position(int(row["id"]), result="yes",
                                            payout_usd=0.0)
        self.assertTrue(first["ok"])
        self.assertAlmostEqual(first["pnl_usd"], -2.59, places=2)
        second = pool.settle_kalshi_position(int(row["id"]), result="yes",
                                             payout_usd=0.0)
        self.assertFalse(second["ok"])
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(float(account["cash_usd"]), 997.41, places=2)
        self.assertAlmostEqual(float(account["reserved_usd"]), 0.0, places=2)

    def test_unfilled_returns_everything(self) -> None:
        self._deploy(ALICE, 500.0, 100.0)
        pool.open_kalshi_placing(
            "k_test:3:1001", ALICE, "kalshi_wick",
            market_ticker="KXBTC-TEST", side="yes", limit_cents=80,
            fee_per_contract_usd=0.02,
        )
        pool.finish_kalshi_open(
            "k_test:3:1001", filled_contracts=0, entry_cents=0,
            cost_usd=0, fee_usd=0, order_id="ord-3",
        )
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(float(account["reserved_usd"]), 0.0, places=2)
        self.assertAlmostEqual(float(account["cash_usd"]), 500.0, places=2)

    def test_no_allocation_and_dust_refuse(self) -> None:
        self._fund(ALICE, 500.0)
        refused = pool.open_kalshi_placing(
            "k_test:4:1001", ALICE, "kalshi_wick",
            market_ticker="KXBTC-TEST", side="yes", limit_cents=70,
            fee_per_contract_usd=0.02,
        )
        self.assertEqual(refused["reason"], "no_allocation")

        pool.set_allocation(ALICE, "kalshi_wick", 10.0)
        # budget = $0.50 — cannot afford one 70¢ contract. Refused, not rounded.
        refused = pool.open_kalshi_placing(
            "k_test:5:1001", ALICE, "kalshi_wick",
            market_ticker="KXBTC-TEST", side="yes", limit_cents=70,
            fee_per_contract_usd=0.02,
        )
        self.assertEqual(refused["reason"], "budget_too_small")
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(float(account["reserved_usd"]), 0.0, places=2)

    def test_double_accept_same_ref_refused(self) -> None:
        self._deploy(ALICE, 1000.0, 200.0)
        first = pool.open_kalshi_placing(
            "k_test:6:1001", ALICE, "kalshi_wick",
            market_ticker="KXBTC-TEST", side="yes", limit_cents=70,
            fee_per_contract_usd=0.02,
        )
        self.assertTrue(first["ok"])
        second = pool.open_kalshi_placing(
            "k_test:6:1001", ALICE, "kalshi_wick",
            market_ticker="KXBTC-TEST", side="yes", limit_cents=70,
            fee_per_contract_usd=0.02,
        )
        self.assertEqual(second["reason"], "already_recorded")
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(float(account["reserved_usd"]),
                               float(first["max_cost_usd"]), places=2)

    def test_journal_reconstructs_the_lifecycle(self) -> None:
        """Every kalshi move is a pool_events row whose running balances agree."""
        self._deploy(ALICE, 1000.0, 200.0)
        pool.open_kalshi_placing(
            "k_test:7:1001", ALICE, "kalshi_wick",
            market_ticker="KXBTC-TEST", side="yes", limit_cents=70,
            fee_per_contract_usd=0.02,
        )
        pool.finish_kalshi_open(
            "k_test:7:1001", filled_contracts=13, entry_cents=70.0,
            cost_usd=9.25, fee_usd=0.15, order_id="ord-7",
        )
        row = pool.open_kalshi_rows("open")[0]
        pool.settle_kalshi_position(int(row["id"]), result="no", payout_usd=0.0)

        portfolio = pool.portfolio(ALICE)
        self.assertEqual(len(portfolio["kalshi_closed"]), 1)
        self.assertAlmostEqual(
            float(portfolio["kalshi_closed"][0]["pnl_usd"]), -9.25, places=2
        )
        self.assertAlmostEqual(portfolio["realized_pnl_usd"], -9.25, places=2)
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(float(account["cash_usd"]), 990.75, places=2)
        self.assertAlmostEqual(float(account["reserved_usd"]), 0.0, places=2)


# ---------------------------------------------------------------------------
# Treasury journal + reconcile total
# ---------------------------------------------------------------------------

class TreasuryTests(Phase1TestCase):
    def test_transfer_lifecycle(self) -> None:
        req = treasury.request_transfer(
            "test_wallet", "coinbase", 500.0, admin_id=ADMIN
        )
        self.assertTrue(req["ok"], req)
        tid = req["transfer_id"]
        self.assertEqual(treasury.in_flight_usd(), 0.0)

        self.assertTrue(treasury.mark_sent(tid, txid=txhash("1"),
                                           admin_id=ADMIN)["ok"])
        self.assertEqual(treasury.in_flight_usd(), 500.0)

        self.assertTrue(treasury.confirm_transfer(tid, admin_id=ADMIN)["ok"])
        self.assertEqual(treasury.in_flight_usd(), 0.0)
        self.assertEqual(treasury.get_transfer(tid)["status"], "confirmed")

    def test_sent_cannot_be_cancelled(self) -> None:
        tid = treasury.request_transfer(
            "test_wallet", "kalshi", 100.0, admin_id=ADMIN
        )["transfer_id"]
        treasury.mark_sent(tid, txid=None, admin_id=ADMIN)
        refused = treasury.cancel_transfer(tid, admin_id=ADMIN)
        self.assertFalse(refused["ok"])
        self.assertEqual(treasury.get_transfer(tid)["status"], "sent")

    def test_bad_locations_refused(self) -> None:
        self.assertFalse(treasury.request_transfer(
            "test_wallet", "test_wallet", 10.0, admin_id=ADMIN)["ok"])
        self.assertFalse(treasury.request_transfer(
            "mattress", "coinbase", 10.0, admin_id=ADMIN)["ok"])
        self.assertFalse(treasury.request_transfer(
            "test_wallet", "coinbase", -5.0, admin_id=ADMIN)["ok"])

    def test_reconcile_total_sums_configured_legs_plus_in_flight(self) -> None:
        tid = treasury.request_transfer(
            "test_wallet", "coinbase", 50.0, admin_id=ADMIN
        )["transfer_id"]
        treasury.mark_sent(tid, txid=txhash("2"), admin_id=ADMIN)
        legs = {
            "test_wallet": {"configured": True, "usd": 400.0, "error": None},
            "coinbase": {"configured": True, "usd": 1500.0, "error": None},
            "kalshi": {"configured": False, "usd": None, "error": None},
        }
        with patch.object(treasury, "balances", return_value=legs):
            total = treasury.reconcile_total()
        self.assertTrue(total["ok"], total)
        self.assertAlmostEqual(total["total_usd"], 1950.0, places=2)
        self.assertAlmostEqual(total["breakdown"]["in_flight_usd"], 50.0)

    def test_test_wallet_leg_sums_every_watched_chain(self) -> None:
        """One EOA, two chains: the treasury figure is the sum, and a single
        unreadable chain makes the leg unknown rather than understated."""
        import chain

        reads = {8453: 400.0, 1: 25.5}
        with patch.object(config, "TEST_WALLET_CHAIN_IDS", (8453, 1)), \
             patch.object(chain, "usdc_balance",
                          side_effect=lambda addr, *, chain_id: reads[chain_id]), \
             patch.object(config, "EXECUTION_MODE", "paper"):
            leg = treasury.balances()["test_wallet"]
        self.assertEqual(leg["usd"], 425.5)
        self.assertEqual(leg["per_chain"], {"Base": 400.0, "Ethereum": 25.5})

        def flaky(addr, *, chain_id):
            if chain_id == 1:
                raise chain.ChainError("rate limited")
            return 400.0

        with patch.object(config, "TEST_WALLET_CHAIN_IDS", (8453, 1)), \
             patch.object(chain, "usdc_balance", side_effect=flaky), \
             patch.object(config, "EXECUTION_MODE", "paper"):
            leg = treasury.balances()["test_wallet"]
        self.assertIsNone(leg["usd"])
        self.assertIn("Ethereum", leg["error"])

    def test_reconcile_total_refuses_on_unreadable_configured_leg(self) -> None:
        legs = {
            "test_wallet": {"configured": True, "usd": None, "error": "boom"},
            "coinbase": {"configured": True, "usd": 1500.0, "error": None},
            "kalshi": {"configured": False, "usd": None, "error": None},
        }
        with patch.object(treasury, "balances", return_value=legs):
            total = treasury.reconcile_total()
        self.assertFalse(total["ok"])
        self.assertEqual(total["reason"], "test_wallet_unreadable")

    def test_venue_demand_follows_the_catalog(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.set_allocation(ALICE, "ict", 300.0)
        pool.set_allocation(ALICE, "kalshi_wick", 100.0)
        demand = treasury.venue_demand()
        self.assertEqual(demand["coinbase"], 300.0)
        self.assertEqual(demand["kalshi"], 100.0)

    def test_ensure_kalshi_coverage_journals_a_shortfall(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.set_allocation(ALICE, "kalshi_wick", 500.0)
        legs = {
            "test_wallet": {"configured": True, "usd": 800.0, "error": None},
            "coinbase": {"configured": False, "usd": None, "error": None},
            "kalshi": {"configured": True, "usd": 100.0, "error": None},
        }
        with patch.object(treasury, "balances", return_value=legs), \
                patch.object(config, "TEST_WALLET_ADDRESS", "0x" + "ab" * 20):
            first = treasury.ensure_kalshi_coverage(
                admin_id=ADMIN, note="auto: test"
            )
            second = treasury.ensure_kalshi_coverage(
                admin_id=ADMIN, note="auto: again"
            )
        self.assertTrue(first["ok"], first)
        self.assertTrue(first["created"])
        self.assertEqual(first["shortfall_usd"], 400.0)
        self.assertEqual(first["from_loc"], "test_wallet")
        # Second call reuses the open pending transfer rather than stacking.
        self.assertFalse(second["created"])
        self.assertEqual(second["transfer_id"], first["transfer_id"])
        row = treasury.get_transfer(int(first["transfer_id"]))
        self.assertEqual(row["status"], "pending_send")
        self.assertEqual(row["to_loc"], "kalshi")
        self.assertAlmostEqual(float(row["amount_usd"]), 400.0, places=2)

    def test_ensure_kalshi_coverage_is_quiet_when_covered(self) -> None:
        self._fund(ALICE, 200.0)
        pool.set_allocation(ALICE, "kalshi_wick", 100.0)
        legs = {
            "test_wallet": {"configured": True, "usd": 50.0, "error": None},
            "coinbase": {"configured": False, "usd": None, "error": None},
            "kalshi": {"configured": True, "usd": 250.0, "error": None},
        }
        with patch.object(treasury, "balances", return_value=legs):
            gap = treasury.ensure_kalshi_coverage(admin_id=ADMIN)
        self.assertTrue(gap["ok"])
        self.assertEqual(gap["shortfall_usd"], 0.0)
        self.assertFalse(gap.get("created"))
        self.assertIsNone(gap.get("transfer_id"))
        self.assertEqual(treasury.open_transfers(), [])

    def test_set_allocation_reports_the_delta(self) -> None:
        self._fund(ALICE, 1000.0)
        first = pool.set_allocation(ALICE, "kalshi_wick", 300.0)
        self.assertEqual(first["previous_usd"], 0.0)
        self.assertEqual(first["delta_usd"], 300.0)
        second = pool.set_allocation(ALICE, "kalshi_wick", 120.0)
        self.assertEqual(second["previous_usd"], 300.0)
        self.assertEqual(second["delta_usd"], -180.0)

    def test_deploy_move_routes_delta_to_the_strategy_venue(self) -> None:
        """Deployed money goes to the venue, undeployed money comes back."""
        covered = {
            "ok": True,
            "deployable_usd": 10_000.0,
            "on_chain_usd": 10_000.0,
            "pending_out_usd": 0.0,
            "undeployed_claims_usd": 0.0,
            "gas_reserve_usd": 0.0,
        }
        with patch.object(config, "TEST_WALLET_ADDRESS", "0x" + "ab" * 20), \
             patch.object(treasury, "deployable_usd", return_value=covered):
            kalshi = treasury.journal_deploy_move(
                telegram_id=ALICE, strategy="kalshi_wick",
                delta_usd=250.0, admin_id=ADMIN,
            )
            coinbase = treasury.journal_deploy_move(
                telegram_id=ALICE, strategy="ict",
                delta_usd=100.0, admin_id=ADMIN,
            )
            back = treasury.journal_deploy_move(
                telegram_id=ALICE, strategy="kalshi_wick",
                delta_usd=-75.0, admin_id=ADMIN,
            )
        self.assertTrue(kalshi["ok"], kalshi)
        self.assertEqual((kalshi["from_loc"], kalshi["to_loc"]), ("test_wallet", "kalshi"))
        self.assertEqual(kalshi["amount_usd"], 250.0)
        self.assertEqual(kalshi["verb"], "deploy")
        self.assertTrue(coinbase["ok"], coinbase)
        self.assertEqual((coinbase["from_loc"], coinbase["to_loc"]), ("test_wallet", "coinbase"))
        self.assertTrue(back["ok"], back)
        self.assertEqual((back["from_loc"], back["to_loc"]), ("kalshi", "test_wallet"))
        self.assertEqual(back["amount_usd"], 75.0)
        self.assertEqual(back["verb"], "undeploy")
        # Every move is its own pending_send row, not folded into a shortfall.
        rows = treasury.open_transfers()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["status"] == "pending_send" for r in rows))
        self.assertIn(f"user {ALICE}", str(rows[0]["note"]))

    def test_deploy_move_refuses_when_intake_cannot_cover(self) -> None:
        short = {
            "ok": True,
            "deployable_usd": 475.0,
            "on_chain_usd": 496.0,
            "pending_out_usd": 0.0,
            "undeployed_claims_usd": 21.0,
            "gas_reserve_usd": 0.0,
        }
        with patch.object(config, "TEST_WALLET_ADDRESS", "0x" + "ab" * 20), \
             patch.object(treasury, "deployable_usd", return_value=short):
            blocked = treasury.journal_deploy_move(
                telegram_id=ALICE, strategy="kalshi_wick",
                delta_usd=500.0, admin_id=ADMIN,
            )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "intake_short")
        self.assertEqual(blocked["need_usd"], 500.0)
        self.assertEqual(blocked["deployable_usd"], 475.0)
        self.assertEqual(treasury.open_transfers(), [])

    def test_deploy_move_is_quiet_when_nothing_moves(self) -> None:
        with patch.object(config, "TEST_WALLET_ADDRESS", "0x" + "ab" * 20):
            same = treasury.journal_deploy_move(
                telegram_id=ALICE, strategy="kalshi_wick",
                delta_usd=0.0, admin_id=ADMIN,
            )
        self.assertEqual(same["reason"], "no_change")
        with patch.object(config, "TEST_WALLET_ADDRESS", ""):
            no_wallet = treasury.journal_deploy_move(
                telegram_id=ALICE, strategy="kalshi_wick",
                delta_usd=50.0, admin_id=ADMIN,
            )
        self.assertEqual(no_wallet["reason"], "no_intake_wallet")
        self.assertEqual(treasury.open_transfers(), [])


# ---------------------------------------------------------------------------
# kalshi_execute.accept — venue choreography with the gateway mocked
# ---------------------------------------------------------------------------

class KalshiAcceptTests(Phase1TestCase):
    def _deploy(self, uid: int) -> None:
        self._fund(uid, 1000.0)
        pool.set_allocation(uid, "kalshi_wick", 200.0)

    def _card(self) -> dict:
        return {"ok": True, "market_ticker": "KXBTC-TEST", "side": "yes",
                "entry_cents": 70.0, "position_id": 42}

    def test_slipped_quote_is_refused_with_nothing_reserved(self) -> None:
        import kalshi_execute
        import kalshi_gateway

        self._deploy(ALICE)
        with (
            patch.object(kalshi_execute, "enabled", return_value=True),
            patch.object(kalshi_execute, "resolve_card",
                         return_value=self._card()),
            patch.object(kalshi_gateway, "get_market",
                         return_value={"status": "active", "yes_ask": 80}),
        ):
            result = kalshi_execute.accept(ALICE, "kalshi_wick", "42")
        self.assertEqual(result["reason"], "slipped")
        self.assertAlmostEqual(
            float(pool.get_account(ALICE)["reserved_usd"]), 0.0, places=2
        )

    def test_happy_path_books_the_actual_fill(self) -> None:
        import kalshi_execute
        import kalshi_gateway

        self._deploy(ALICE)
        with (
            patch.object(kalshi_execute, "enabled", return_value=True),
            patch.object(kalshi_execute, "resolve_card",
                         return_value=self._card()),
            patch.object(kalshi_gateway, "get_market",
                         return_value={"status": "active", "yes_ask": 71}),
            patch.object(kalshi_gateway, "place_limit_buy",
                         return_value={"order_id": "ord-9"}),
            patch.object(kalshi_gateway, "get_order",
                         return_value={"remaining_count": 0,
                                       "status": "executed"}),
            patch.object(kalshi_gateway, "fill_summary",
                         return_value={"contracts": 13, "avg_cents": 71.0,
                                       "cost_usd": 9.38, "fee_usd": 0.15}),
        ):
            result = kalshi_execute.accept(ALICE, "kalshi_wick", "42")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["contracts"], 13)
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(float(account["reserved_usd"]), 9.38, places=2)
        rows = pool.open_kalshi_rows("open")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_id"], "ord-9")

    def test_order_refused_releases_the_reserve(self) -> None:
        import kalshi_execute
        import kalshi_gateway

        self._deploy(ALICE)
        with (
            patch.object(kalshi_execute, "enabled", return_value=True),
            patch.object(kalshi_execute, "resolve_card",
                         return_value=self._card()),
            patch.object(kalshi_gateway, "get_market",
                         return_value={"status": "active", "yes_ask": 70}),
            patch.object(kalshi_gateway, "place_limit_buy",
                         side_effect=kalshi_gateway.KalshiError("nope")),
        ):
            result = kalshi_execute.accept(ALICE, "kalshi_wick", "42")
        self.assertEqual(result["reason"], "order_refused")
        self.assertAlmostEqual(
            float(pool.get_account(ALICE)["reserved_usd"]), 0.0, places=2
        )

    def test_unknown_fill_state_keeps_the_reserve(self) -> None:
        import kalshi_execute
        import kalshi_gateway

        self._deploy(ALICE)
        with (
            patch.object(kalshi_execute, "enabled", return_value=True),
            patch.object(kalshi_execute, "resolve_card",
                         return_value=self._card()),
            patch.object(kalshi_gateway, "get_market",
                         return_value={"status": "active", "yes_ask": 70}),
            patch.object(kalshi_gateway, "place_limit_buy",
                         return_value={"order_id": "ord-10"}),
            patch.object(kalshi_gateway, "get_order",
                         side_effect=kalshi_gateway.KalshiError("timeout")),
        ):
            result = kalshi_execute.accept(ALICE, "kalshi_wick", "42")
        self.assertEqual(result["reason"], "fill_unknown")
        # The reserve is deliberately still held: an order may exist at the
        # venue, and releasing would free money that could be in contracts.
        self.assertGreater(
            float(pool.get_account(ALICE)["reserved_usd"]), 0.0
        )
        self.assertEqual(len(pool.open_kalshi_rows("placing")), 1)


# ---------------------------------------------------------------------------
# Flat deploy network fee (Ship 1)
# ---------------------------------------------------------------------------

class DeployFeeTests(Phase1TestCase):
    """Fee from undeployed cash; full allocation still goes to the venue."""

    def setUp(self) -> None:
        super().setUp()
        # Override the fixture's fee=0 so these cases exercise the live default.
        self._fee_patch = patch.object(bot_config, "POOL_DEPLOY_FEE_USD", 5.0)
        self._fee_patch.start()
        self.addCleanup(self._fee_patch.stop)

    def test_increase_requires_fee_room_and_does_not_shrink_allocation(self) -> None:
        self._fund(ALICE, 505.0)
        blocked = pool.set_allocation(ALICE, "kalshi_wick", 505.0)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "exceeds_wallet")
        self.assertEqual(blocked["fee_usd"], 5.0)
        self.assertEqual(blocked["max_usd"], 500.0)

        ok = pool.set_allocation(ALICE, "kalshi_wick", 500.0)
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["amount_usd"], 500.0)
        self.assertEqual(ok["fee_usd"], 5.0)
        self.assertEqual(ok["delta_usd"], 500.0)
        # Soft-lock is the full venue amount; cash is unchanged until charge.
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 505.0)
        self.assertEqual(pool.get_allocation(ALICE, "kalshi_wick"), 500.0)

    def test_charge_deploy_fee_leaves_allocation_intact(self) -> None:
        self._fund(ALICE, 505.0)
        self.assertTrue(pool.set_allocation(ALICE, "kalshi_wick", 500.0)["ok"])
        charged = pool.charge_deploy_fee(
            ALICE, strategy="kalshi_wick", transfer_id=42,
        )
        self.assertTrue(charged["ok"], charged)
        self.assertEqual(charged["fee_usd"], 5.0)
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 500.0)
        self.assertEqual(pool.get_allocation(ALICE, "kalshi_wick"), 500.0)
        # Undeployed claim is gone; the $5 sits as house float on-chain.
        self.assertEqual(pool.wallet_balance(ALICE)["wallet_usd"], 0.0)

        again = pool.charge_deploy_fee(
            ALICE, strategy="kalshi_wick", transfer_id=42,
        )
        self.assertTrue(again["ok"], again)
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 500.0)

    def test_undeploy_and_no_change_are_free(self) -> None:
        self._fund(ALICE, 505.0)
        self.assertTrue(pool.set_allocation(ALICE, "kalshi_wick", 500.0)["ok"])
        pool.charge_deploy_fee(ALICE, strategy="kalshi_wick", transfer_id=7)
        lowered = pool.set_allocation(ALICE, "kalshi_wick", 200.0)
        self.assertTrue(lowered["ok"], lowered)
        self.assertEqual(lowered["fee_usd"], 0.0)
        same = pool.set_allocation(ALICE, "kalshi_wick", 200.0)
        self.assertTrue(same["ok"], same)
        self.assertEqual(same["fee_usd"], 0.0)
        self.assertEqual(same["delta_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
