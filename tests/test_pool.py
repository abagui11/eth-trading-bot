"""Tester pool — the fiduciary invariants.

Every dollar a tester sees must be reconstructable from pool_events, exits
must never double-book, and the pro-rata split must conserve money: the sum
of everyone's shares equals the trade, always. These tests pin those
properties, plus the access gate and the reconcile freeze.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import access
import bot_config
import config
import pool

ADMIN = 111
ALICE = 1001
BOB = 1002

# Tests run with a deposit address configured, the way production does, so the
# txid rules are exercised rather than skipped.
ADDRESS = "0x6549B1E2C9B3b004fca5E3C13AD8189Cf2f273B1"


def txhash(seed: str) -> str:
    """A well-formed 32-byte tx hash, distinct per seed."""
    return "0x" + (seed * 64)[:64]


class PoolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "POOL_DEPOSIT_ADDRESS", ADDRESS),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_RISK_PCT", 0.007),
            patch.object(bot_config, "POOL_MIN_EQUITY_USD", 500.0),
            patch.object(bot_config, "POOL_MIN_DEPOSIT_USD", 500.0),
            patch.object(bot_config, "POOL_RECON_TOLERANCE_USD", 25.0),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self._db = db
        pool.init_db()

    def _fund(self, uid: int, amount: float) -> None:
        pool.approve_user(uid, admin_id=ADMIN)
        result = pool.credit(uid, amount, admin_id=ADMIN)
        self.assertTrue(result["ok"], result)


class AccessTests(PoolTestCase):
    def test_first_contact_is_new_then_pending(self) -> None:
        self.assertEqual(pool.request_access(ALICE, "alice"), "new")
        self.assertEqual(pool.request_access(ALICE, "alice"), "pending")
        self.assertFalse(pool.is_approved(ALICE))

    def test_approve_opens_a_zero_balance_account(self) -> None:
        pool.request_access(ALICE, "alice")
        pool.approve_user(ALICE, admin_id=ADMIN)
        self.assertTrue(pool.is_approved(ALICE))
        account = pool.get_account(ALICE)
        self.assertIsNotNone(account)
        self.assertEqual(float(account["cash_usd"]), 0.0)
        self.assertFalse(pool.is_funded(ALICE))

    def test_admin_ids_merge_env_and_code(self) -> None:
        """Ops must be able to add an admin via .env without a deploy.

        An empty result is the dangerous case: Admit cards would go nowhere
        and nobody could ever be let into the product.
        """
        with patch.object(config, "POOL_ADMIN_TELEGRAM_IDS", [999]):
            self.assertEqual(pool.admin_ids(), [ADMIN, 999])

        with patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", ()):
            with patch.object(config, "POOL_ADMIN_TELEGRAM_IDS", [999]):
                self.assertEqual(pool.admin_ids(), [999])
            with patch.object(config, "POOL_ADMIN_TELEGRAM_IDS", []):
                with patch.object(config, "INTERNAL_TELEGRAM_IDS", [777]):
                    self.assertEqual(pool.admin_ids(), [777])

    def test_deny_sticks(self) -> None:
        pool.request_access(BOB)
        pool.deny_user(BOB, admin_id=ADMIN)
        self.assertEqual(pool.request_access(BOB), "denied")
        self.assertFalse(pool.is_approved(BOB))

    def test_is_allowed_gates_unknown_users_when_pool_is_on(self) -> None:
        with patch.object(config, "PAYWALL_ENABLED", False):
            self.assertFalse(access.is_allowed(999999))
            pool.approve_user(999999, admin_id=ADMIN)
            self.assertTrue(access.is_allowed(999999))

    def test_pool_off_keeps_open_access(self) -> None:
        with patch.object(bot_config, "POOL_ENABLED", False), patch.object(
            config, "PAYWALL_ENABLED", False
        ):
            self.assertTrue(access.is_allowed(999999))


class CashJournalTests(PoolTestCase):
    def test_credit_moves_cash_and_journals_the_balance(self) -> None:
        self._fund(ALICE, 1000.0)
        account = pool.get_account(ALICE)
        self.assertEqual(float(account["cash_usd"]), 1000.0)
        self.assertEqual(float(account["deposited_usd"]), 1000.0)
        conn = sqlite3.connect(self._db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM pool_events WHERE telegram_id = ?", (ALICE,)
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "deposit")
        self.assertEqual(float(rows[0]["cash_after"]), 1000.0)

    def test_duplicate_credit_ref_is_a_noop(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        first = pool.credit(ALICE, 500.0, admin_id=ADMIN, ref="dep:1")
        second = pool.credit(ALICE, 500.0, admin_id=ADMIN, ref="dep:1")
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "duplicate")
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 500.0)

    def test_debit_cannot_touch_reserved_margin(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.record_intent("hq-x", ALICE)
        reserved = float(pool.get_account(ALICE)["reserved_usd"])
        self.assertGreater(reserved, 0)
        result = pool.debit(ALICE, 1000.0, admin_id=ADMIN)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "insufficient_available")
        ok = pool.debit(ALICE, 1000.0 - reserved, admin_id=ADMIN)
        self.assertTrue(ok["ok"])


class DepositRequestTests(PoolTestCase):
    def test_request_and_one_tap_credit(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        req = pool.request_deposit(ALICE, 800.0, txid=txhash("a"))
        self.assertTrue(req["ok"])
        result = pool.decide_deposit(req["request_id"], admin_id=ADMIN, approve=True)
        self.assertEqual(result["status"], "credited")
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 800.0)
        # Deciding twice cannot credit twice.
        again = pool.decide_deposit(req["request_id"], admin_id=ADMIN, approve=True)
        self.assertFalse(again["ok"])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 800.0)

    def test_below_minimum_and_double_pending_are_refused(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        self.assertEqual(
            pool.request_deposit(ALICE, 100.0, txid=txhash("a"))["reason"],
            "below_minimum",
        )
        first = pool.request_deposit(ALICE, 600.0, txid=txhash("b"))
        self.assertTrue(first["ok"])
        second = pool.request_deposit(ALICE, 700.0, txid=txhash("c"))
        self.assertEqual(second["reason"], "already_pending")

    def test_denied_request_moves_no_money(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        req = pool.request_deposit(ALICE, 600.0, txid=txhash("d"))
        result = pool.decide_deposit(req["request_id"], admin_id=ADMIN, approve=False)
        self.assertEqual(result["status"], "denied")
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 0.0)


class DepositTxidTests(PoolTestCase):
    """The deposit address is shared with the yield wallet, so the hash is the
    only thing tying a claimed amount to a transfer that actually arrived."""

    def setUp(self) -> None:
        super().setUp()
        pool.approve_user(ALICE, admin_id=ADMIN)
        pool.approve_user(BOB, admin_id=ADMIN)

    def test_a_hash_is_required_once_an_address_is_configured(self) -> None:
        self.assertEqual(
            pool.request_deposit(ALICE, 600.0)["reason"], "txid_required"
        )
        self.assertEqual(
            pool.request_deposit(ALICE, 600.0, txid="not-a-hash")["reason"],
            "txid_malformed",
        )

    def test_no_address_configured_keeps_the_hash_optional(self) -> None:
        with patch.object(config, "POOL_DEPOSIT_ADDRESS", None):
            self.assertTrue(pool.request_deposit(ALICE, 600.0)["ok"])

    def test_one_transfer_cannot_be_claimed_twice(self) -> None:
        """The failure this prevents: two testers credited for one deposit,
        so the second one's balance is really the first one's money."""
        h = txhash("e")
        self.assertTrue(pool.request_deposit(ALICE, 600.0, txid=h)["ok"])

        stolen = pool.request_deposit(BOB, 600.0, txid=h)
        self.assertEqual(stolen["reason"], "txid_already_claimed")
        self.assertEqual(stolen["claimed_by"], ALICE)

        # Still blocked after the first one is credited, not just while pending.
        pool.decide_deposit(
            pool.get_deposit_request(1)["id"], admin_id=ADMIN, approve=True
        )
        self.assertEqual(
            pool.request_deposit(BOB, 600.0, txid=h)["reason"],
            "txid_already_claimed",
        )
        self.assertEqual(float(pool.get_account(BOB)["cash_usd"]), 0.0)

    def test_case_and_prefix_do_not_launder_a_duplicate(self) -> None:
        h = txhash("f")
        pool.request_deposit(ALICE, 600.0, txid=h)
        for variant in (h.upper().replace("0X", "0x"), h[2:], f"  {h}  "):
            self.assertEqual(
                pool.request_deposit(BOB, 600.0, txid=variant)["reason"],
                "txid_already_claimed",
                f"variant {variant!r} slipped past the uniqueness check",
            )

    def test_a_denied_hash_can_be_refiled(self) -> None:
        """A typo'd amount is denied, so the real transfer must still be
        creditable under its own hash."""
        h = txhash("1")
        first = pool.request_deposit(ALICE, 600.0, txid=h)
        pool.decide_deposit(first["request_id"], admin_id=ADMIN, approve=False)
        again = pool.request_deposit(ALICE, 900.0, txid=h)
        self.assertTrue(again["ok"], again)

    def test_pending_inbound_is_what_the_wallet_owes_testers(self) -> None:
        self.assertEqual(pool.pending_inbound_usd(), 0.0)
        a = pool.request_deposit(ALICE, 600.0, txid=txhash("2"))
        pool.request_deposit(BOB, 900.0, txid=txhash("3"))
        self.assertEqual(pool.pending_inbound_usd(), 1500.0)

        # Crediting sweeps it out of "sitting in the wallet" and into cash.
        pool.decide_deposit(a["request_id"], admin_id=ADMIN, approve=True)
        self.assertEqual(pool.pending_inbound_usd(), 900.0)


class IntentTests(PoolTestCase):
    def test_intent_reserves_the_risk_budget(self) -> None:
        self._fund(ALICE, 1000.0)
        result = pool.record_intent("hq-1", ALICE)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["risk_usd"], 7.0, places=2)
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(float(account["reserved_usd"]), 7.0, places=2)
        # Cash is untouched — only availability moved.
        self.assertEqual(float(account["cash_usd"]), 1000.0)

    def test_second_intent_sizes_off_what_is_left(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.record_intent("hq-1", ALICE)
        second = pool.record_intent("hq-2", ALICE)
        self.assertTrue(second["ok"])
        self.assertAlmostEqual(second["risk_usd"], (1000.0 - 7.0) * 0.007, places=2)

    def test_duplicate_and_underfunded_refused(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.record_intent("hq-1", ALICE)
        self.assertEqual(
            pool.record_intent("hq-1", ALICE)["reason"], "already_recorded"
        )
        self._fund(BOB, 400.0)  # below POOL_MIN_EQUITY_USD
        self.assertEqual(
            pool.record_intent("hq-1", BOB)["reason"], "below_min_equity"
        )

    def test_frozen_pool_refuses_new_intents(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.freeze_intents("test")
        self.assertEqual(pool.record_intent("hq-9", ALICE)["reason"], "frozen")
        pool.unfreeze_intents()
        self.assertTrue(pool.record_intent("hq-9", ALICE)["ok"])

    def test_stale_intents_release_their_reserve(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.record_intent("hq-old", ALICE)
        pool.record_intent("hq-live", ALICE)
        released = pool.expire_stale_intents({"hq-live"})
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["ref"], "hq-old")
        account = pool.get_account(ALICE)
        # Only the live intent's reserve remains.
        live = [i for i in pool.pending_intents("hq-live")][0]
        self.assertAlmostEqual(
            float(account["reserved_usd"]), float(live["risk_usd"]), places=2
        )


class FillMathTests(PoolTestCase):
    """One BTC nano contract = 0.01 BTC. Entry 80,000, stop 79,300 → risk
    per unit $700, per contract $7."""

    ENTRY = 80_000.0
    STOP = 79_300.0
    RPU = 700.0
    FLOOR = 0.01

    def test_extra_contracts_are_whole_only(self) -> None:
        self._fund(ALICE, 1000.0)   # budget $7.00 → exactly 1 contract
        self._fund(BOB, 600.0)      # budget $4.20 → fraction, no extra alone
        pool.record_intent("hq-1", ALICE)
        pool.record_intent("hq-1", BOB)
        extra, intents = pool.extra_contracts_for(
            "hq-1", risk_per_unit=self.RPU, floor=self.FLOOR
        )
        # $7.00 + $4.20 = $11.20 → one whole $7 contract.
        self.assertAlmostEqual(extra, 0.01, places=9)
        self.assertEqual(len(intents), 2)

    def test_split_conserves_the_fill(self) -> None:
        self._fund(ALICE, 1000.0)
        self._fund(BOB, 600.0)
        pool.record_intent("hq-1", ALICE)
        pool.record_intent("hq-1", BOB)
        # House risked 2 contracts ($14) and the pool added 1 → fill 0.03.
        house_risk = 14.0
        fill_qty = 0.03
        stakes = pool.open_stakes(
            7,
            "hq-1",
            fill_qty=fill_qty,
            fill_price=self.ENTRY,
            risk_per_unit=self.RPU,
            house_risk_usd=house_risk,
        )
        self.assertEqual(len(stakes), 2)
        total_budget = house_risk + 7.0 + 4.2
        by_uid = {s["telegram_id"]: s for s in stakes}
        self.assertAlmostEqual(
            by_uid[ALICE]["share_frac"], 7.0 / total_budget, places=6
        )
        self.assertAlmostEqual(
            by_uid[BOB]["share_frac"], 4.2 / total_budget, places=6
        )
        # Tester qty shares + implicit house residual = the whole fill.
        tester_qty = sum(s["qty"] for s in stakes)
        house_qty = fill_qty * (house_risk / total_budget)
        self.assertAlmostEqual(tester_qty + house_qty, fill_qty, places=9)
        # The intent reserve was swapped for stake margin.
        account = pool.get_account(ALICE)
        self.assertAlmostEqual(
            float(account["reserved_usd"]), by_uid[ALICE]["cost_usd"], places=2
        )
        self.assertEqual(len(pool.pending_intents("hq-1")), 0)

    def test_margin_is_trimmed_to_available_cash(self) -> None:
        # Alice's share of a huge fill would cost more cash than she has.
        self._fund(ALICE, 501.0)
        pool.record_intent("hq-1", ALICE)
        stakes = pool.open_stakes(
            8,
            "hq-1",
            fill_qty=1.0,               # $80k notional
            fill_price=self.ENTRY,
            risk_per_unit=self.RPU,
            house_risk_usd=7.0,
        )
        self.assertEqual(len(stakes), 1)
        self.assertLessEqual(stakes[0]["cost_usd"], 501.0 + 1e-6)

    def test_no_intents_means_no_stakes_and_no_extra(self) -> None:
        extra, intents = pool.extra_contracts_for(
            "hq-none", risk_per_unit=self.RPU, floor=self.FLOOR
        )
        self.assertEqual(extra, 0.0)
        self.assertEqual(intents, [])
        self.assertEqual(
            pool.open_stakes(
                9, "hq-none", fill_qty=0.01, fill_price=self.ENTRY,
                risk_per_unit=self.RPU, house_risk_usd=7.0,
            ),
            [],
        )


class ExitBookingTests(PoolTestCase):
    ENTRY = 80_000.0
    RPU = 700.0

    def _open(self, trade_id: int = 21, fill_qty: float = 0.03) -> dict:
        self._fund(ALICE, 1000.0)
        pool.record_intent("hq-1", ALICE)
        stakes = pool.open_stakes(
            trade_id, "hq-1",
            fill_qty=fill_qty, fill_price=self.ENTRY,
            risk_per_unit=self.RPU, house_risk_usd=14.0,
        )
        return stakes[0]

    def test_partial_exit_credits_the_share_and_is_idempotent(self) -> None:
        stake = self._open()
        booked = pool.book_exit(
            21, exit_qty=0.01, exit_price=80_300.0, pnl_usd=3.0,
            order_id="ord-1", reason="take_profit", qty_total=0.03,
        )
        self.assertEqual(len(booked), 1)
        expected = round(3.0 * stake["share_frac"], 2)
        self.assertAlmostEqual(booked[0]["pnl_usd"], expected, places=2)
        cash = float(pool.get_account(ALICE)["cash_usd"])
        self.assertAlmostEqual(cash, 1000.0 + expected, places=2)
        # Sweeping the same settled order again books nothing.
        again = pool.book_exit(
            21, exit_qty=0.01, exit_price=80_300.0, pnl_usd=3.0,
            order_id="ord-1", reason="take_profit", qty_total=0.03,
        )
        self.assertEqual(again, [])
        self.assertAlmostEqual(
            float(pool.get_account(ALICE)["cash_usd"]), cash, places=2
        )

    def test_full_cycle_returns_cash_plus_pnl_share(self) -> None:
        stake = self._open()
        pool.book_exit(
            21, exit_qty=0.01, exit_price=80_300.0, pnl_usd=3.0,
            order_id="ord-1", reason="take_profit", qty_total=0.03,
        )
        pool.book_exit(
            21, exit_qty=0.02, exit_price=79_300.0, pnl_usd=-14.0,
            order_id="ord-2", reason="stop_loss", qty_total=0.03,
        )
        pool.book_close(21, close_reason="stop_loss")
        account = pool.get_account(ALICE)
        share = stake["share_frac"]
        expected_cash = 1000.0 + round(3.0 * share, 2) + round(-14.0 * share, 2)
        self.assertAlmostEqual(float(account["cash_usd"]), expected_cash, places=2)
        # Every reserve is released once the trade is closed.
        self.assertAlmostEqual(float(account["reserved_usd"]), 0.0, places=2)
        stakes = pool.stakes_for(21)
        self.assertEqual(stakes[0]["status"], "closed")

    def test_close_without_stakes_is_harmless(self) -> None:
        self.assertEqual(pool.book_close(999, close_reason="stop_loss"), [])


class ReconcileTests(PoolTestCase):
    def test_covered_book_stays_open(self) -> None:
        self._fund(ALICE, 1000.0)
        snapshot = pool.reconcile(1500.0)
        self.assertTrue(snapshot["ok"])
        self.assertIsNone(pool.intents_frozen())

    def test_shortfall_freezes_new_intents_only(self) -> None:
        self._fund(ALICE, 1000.0)
        snapshot = pool.reconcile(900.0)  # cannot cover Alice's $1,000 claim
        self.assertFalse(snapshot["ok"])
        self.assertIsNotNone(pool.intents_frozen())
        self.assertEqual(pool.record_intent("hq-1", ALICE)["reason"], "frozen")
        # Exits still book: fiduciary freeze stops NEW risk, not accounting.
        pool.approve_user(BOB, admin_id=ADMIN)
        self.assertTrue(pool.credit(BOB, 600.0, admin_id=ADMIN)["ok"])

    def test_tolerance_absorbs_dust(self) -> None:
        self._fund(ALICE, 1000.0)
        snapshot = pool.reconcile(1000.0 - 10.0)  # within $25 tolerance
        self.assertTrue(snapshot["ok"])

    def test_cash_awaiting_transfer_into_futures_is_not_a_shortfall(self) -> None:
        """The bug this replaces.

        Deposits land in the spot wallet, so the futures sleeve's equity can
        be a small fraction of the account. Measuring claims against the
        futures pot alone froze the pool on the first credited deposit of a
        perfectly solvent account: $499.98 equity against a $1,000 claim,
        while spot held $3,578.96.
        """
        self._fund(ALICE, 1000.0)
        snapshot = pool.reconcile(
            3578.96 + 68.67,
            breakdown={"spot_usd": 3578.96, "futures_usd": 68.67, "collateral_usd": 68.67},
        )
        self.assertTrue(snapshot["ok"], snapshot)
        self.assertIsNone(pool.intents_frozen())
        # Collateral is a fraction of the claim and that is fine: where the
        # cash sits is recorded, never a solvency verdict.
        self.assertLess(snapshot["breakdown"]["collateral_usd"], snapshot["tester_cash_usd"])

    def test_the_snapshot_carries_where_the_money_sits(self) -> None:
        self._fund(ALICE, 600.0)
        snapshot = pool.reconcile(
            1000.0,
            breakdown={"spot_usd": 800.0, "futures_usd": 200.0, "buying_power_usd": 950.0},
        )
        self.assertEqual(snapshot["venue_assets_usd"], 1000.0)
        self.assertEqual(snapshot["breakdown"]["spot_usd"], 800.0)
        self.assertEqual(snapshot["house_residual_usd"], 400.0)
        self.assertEqual(pool.last_reconcile()["breakdown"]["futures_usd"], 200.0)


class CashAssetsTests(unittest.TestCase):
    """What the reconciler is allowed to count as backing a tester's claim.

    Payloads are the ones the live account actually returned, because the trap
    here is arithmetic that looks right on invented numbers: Coinbase reports
    the same spot money twice, once as wallets and once as cbi_usd_balance.
    """

    # Observed 2026-09-15 on the production account.
    _V3_ACCOUNTS = {
        "accounts": [
            {"available_balance": {"currency": "USDC", "value": "3353.17"},
             "hold": {"currency": "USDC", "value": "0"}},
            {"available_balance": {"currency": "USD", "value": "225.79"},
             "hold": {"currency": "USD", "value": "0"}},
            # Spot crypto: an asset, but not what a dollar claim is backed by,
            # and pricing it would add a failure mode to a solvency check.
            {"available_balance": {"currency": "BTC", "value": "0.4"},
             "hold": {"currency": "BTC", "value": "0"}},
        ],
        "has_next": False,
    }
    _CFM = {
        "balance_summary": {
            "total_usd_balance": {"value": "499.98"},
            "cbi_usd_balance": {"value": "431.31"},
            "cfm_usd_balance": {"value": "68.67"},
            "futures_buying_power": {"value": "3737.92"},
            "unrealized_pnl": {"value": "-2.35"},
        }
    }

    def _gateway(self, *, v3=None, has_next=False):
        import coinbase_deriv

        gw = coinbase_deriv.DerivGateway()
        accounts = dict(v3 or self._V3_ACCOUNTS)
        accounts["has_next"] = has_next

        def _request(method, path, **kwargs):
            if path.endswith("/accounts"):
                return accounts
            if path.endswith("/cfm/balance_summary"):
                return self._CFM
            raise AssertionError(f"unexpected call {method} {path}")

        gw._request = _request  # type: ignore[method-assign]
        return gw

    def test_spot_wallets_plus_futures_collateral_only(self) -> None:
        assets = self._gateway().get_cash_assets()
        self.assertAlmostEqual(assets["spot_usd"], 3578.96, places=2)
        self.assertAlmostEqual(assets["futures_usd"], 68.67, places=2)
        self.assertAlmostEqual(assets["total_usd"], 3647.63, places=2)

    def test_collateral_and_buying_power_are_reported_separately(self) -> None:
        """Only $68.67 sits in CFM, but Coinbase lends against the spot USDC.

        Reporting the collateral figure alone would read as a desk that is out
        of money while it in fact has $3,737.92 of capacity.
        """
        assets = self._gateway().get_cash_assets()
        self.assertAlmostEqual(assets["collateral_usd"], 68.67, places=2)
        self.assertAlmostEqual(assets["buying_power_usd"], 3737.92, places=2)

    def test_cbi_balance_is_not_added_on_top_of_the_wallets(self) -> None:
        """cbi_usd_balance is a view of the same consumer spot money.

        Counting it as well would report $4,078.94 of backing for $3,647.63 of
        real cash — over-reporting coverage, which is the one direction a
        fiduciary floor must never fail in.
        """
        assets = self._gateway().get_cash_assets()
        self.assertNotAlmostEqual(assets["total_usd"], 3647.63 + 431.31, places=2)
        self.assertNotAlmostEqual(assets["total_usd"], 3578.96 + 499.98, places=2)

    def test_non_cash_crypto_is_excluded(self) -> None:
        assets = self._gateway().get_cash_assets()
        self.assertEqual(sorted(assets["wallets"]), ["USD", "USDC"])

    def test_a_truncated_account_list_is_flagged(self) -> None:
        """A partial page under-reports assets, which reads as a shortfall."""
        self.assertTrue(self._gateway(has_next=True).get_cash_assets()["truncated"])


class ReconcileSkipTests(PoolTestCase):
    def _run_sweep(self, gateway_factory):
        import coinbase_deriv
        import live_pending
        import notify
        import trade_ideas_bridge
        import watchdog

        watchdog._pool_last_recon = 0.0
        with patch.object(config, "EXECUTION_MODE", "live"), \
                patch.object(live_pending, "get_pending", return_value=[]), \
                patch.object(trade_ideas_bridge, "pool_active_mill_refs", return_value=set()), \
                patch.object(pool, "expire_stale_intents", return_value=[]), \
                patch.object(notify, "send_pool_admin_alert"), \
                patch.object(coinbase_deriv, "get_gateway", gateway_factory), \
                patch.object(pool, "reconcile") as reconcile:
            watchdog._pool_sweep()
        return reconcile

    def test_an_unreadable_balance_skips_the_check_instead_of_freezing(self) -> None:
        """Freezing the pool on a transient API error is worse than checking late.

        The previous code read `get_account_summary().get("equity") or 0.0`, so
        any unexpected response shape became $0 of assets and froze every
        tester's Accepts.
        """
        def _boom():
            raise RuntimeError("coinbase 503")

        reconcile = self._run_sweep(_boom)
        reconcile.assert_not_called()
        self.assertIsNone(pool.intents_frozen())

    def test_zero_assets_is_treated_as_unreadable_not_as_insolvent(self) -> None:
        from unittest.mock import MagicMock

        gw = MagicMock()
        gw.get_cash_assets.return_value = {
            "total_usd": 0.0, "spot_usd": 0.0, "futures_usd": 0.0,
            "collateral_usd": 0.0, "buying_power_usd": 0.0,
            "wallets": {}, "truncated": False,
        }
        reconcile = self._run_sweep(lambda: gw)
        reconcile.assert_not_called()

    def test_a_healthy_read_is_judged_on_total_assets(self) -> None:
        from unittest.mock import MagicMock

        gw = MagicMock()
        gw.get_cash_assets.return_value = {
            "total_usd": 3647.63, "spot_usd": 3578.96, "futures_usd": 68.67,
            "collateral_usd": 68.67, "buying_power_usd": 3737.92,
            "wallets": {}, "truncated": False,
        }
        reconcile = self._run_sweep(lambda: gw)
        reconcile.assert_called_once()
        self.assertAlmostEqual(reconcile.call_args.args[0], 3647.63, places=2)
        breakdown = reconcile.call_args.kwargs["breakdown"]
        self.assertAlmostEqual(breakdown["spot_usd"], 3578.96, places=2)
        self.assertAlmostEqual(breakdown["buying_power_usd"], 3737.92, places=2)


class PortfolioTests(PoolTestCase):
    def test_portfolio_reports_the_journal_truthfully(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.record_intent("hq-1", ALICE)
        p = pool.portfolio(ALICE)
        self.assertTrue(p["ok"])
        self.assertEqual(p["cash_usd"], 1000.0)
        self.assertGreater(p["reserved_usd"], 0)
        self.assertEqual(p["deposited_usd"], 1000.0)

    def test_prospective_accept_matches_intent_math(self) -> None:
        self._fund(ALICE, 1000.0)
        # $1,000 × 0.7% = $7 risk; stop $700/unit → ~$800 notional at $80k.
        prosp = pool.prospective_accept(ALICE, entry=80_000.0, stop_loss=79_300.0)
        self.assertTrue(prosp["ok"])
        self.assertAlmostEqual(prosp["risk_usd"], 7.0, places=2)
        self.assertAlmostEqual(prosp["notional_usd"], 7.0 * 80_000.0 / 700.0, places=2)
        intent = pool.record_intent("hq-prospective", ALICE)
        self.assertAlmostEqual(intent["risk_usd"], prosp["risk_usd"], places=2)

    def test_no_account_is_a_clean_answer(self) -> None:
        self.assertFalse(pool.portfolio(424242)["ok"])


if __name__ == "__main__":
    unittest.main()
