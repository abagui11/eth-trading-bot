"""Tester pool — the fiduciary invariants.

Every dollar a tester sees must be reconstructable from pool_events, exits
must never double-book, and the pro-rata split must conserve money: the sum
of everyone's shares equals the trade, always. These tests pin those
properties, plus the access gate and the reconcile freeze.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
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
# wallet and txid rules are exercised rather than skipped. This is the real
# Coinbase USDC deposit address: funds sent here land at the venue directly.
ADDRESS = "0xDdA10FB6e6d726ae1cfB079CD79A4f0Ef7cAF240"


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

    def _wallet(self, uid: int) -> str:
        """Register a distinct sending address for this tester."""
        address = "0x" + f"{uid:040x}"
        result = pool.register_wallet(uid, address)
        self.assertTrue(result["ok"], result)
        return address


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

    def test_list_access_roster_orders_pending_first_with_cash(self) -> None:
        pool.request_access(ALICE, "alice")
        pool.approve_user(ALICE, admin_id=ADMIN, username="alice")
        pool.credit(ALICE, 250.0, admin_id=ADMIN, note="t")
        pool.request_access(BOB, "bob")
        roster = pool.list_access_roster()
        self.assertEqual([r["telegram_id"] for r in roster], [BOB, ALICE])
        self.assertEqual(roster[0]["status"], "pending")
        self.assertEqual(roster[0]["username"], "bob")
        self.assertEqual(roster[1]["status"], "approved")
        self.assertEqual(float(roster[1]["cash_usd"]), 250.0)

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


class WalletRegistrationTests(PoolTestCase):
    """The address a tester funds from is the only address they are paid to.

    Return-to-source is what keeps us out of the business of sending client
    money to destinations nobody has proven they control, so these tests pin
    who may bind an address, what proves it, and how hard it is to move.
    """

    W1 = "0x1111111111111111111111111111111111111111"
    W2 = "0x2222222222222222222222222222222222222222"

    def setUp(self) -> None:
        super().setUp()
        pool.approve_user(ALICE, admin_id=ADMIN)
        pool.approve_user(BOB, admin_id=ADMIN)

    def test_first_registration_is_self_serve_and_starts_unproven(self) -> None:
        result = pool.register_wallet(ALICE, self.W1)
        self.assertTrue(result["ok"], result)
        wallet = pool.get_wallet(ALICE)
        self.assertEqual(wallet["address"], self.W1)
        # Unproven until money arrives from it — a typed address is a claim,
        # not evidence.
        self.assertEqual(wallet["status"], "pending")
        self.assertEqual(pool.payout_target(ALICE)["reason"], "unverified")

    def test_checksummed_and_unprefixed_spellings_are_one_address(self) -> None:
        pool.register_wallet(ALICE, self.W1)
        for variant in (self.W1.upper().replace("0X", "0x"), self.W1[2:],
                        f"  {self.W1}  "):
            self.assertEqual(
                pool.register_wallet(ALICE, variant).get("unchanged"), True,
                f"{variant!r} was treated as a different address",
            )
        self.assertEqual(pool.wallet_owner(self.W1.upper()), ALICE)

    def test_malformed_addresses_are_refused(self) -> None:
        for bad in ("", "0x", "nope", self.W1 + "ff", self.W1[:-1],
                    "0xzzzz111111111111111111111111111111111111"):
            self.assertEqual(
                pool.register_wallet(ALICE, bad).get("reason"), "malformed",
                f"{bad!r} was accepted as an address",
            )

    def test_two_testers_cannot_claim_one_address(self) -> None:
        """Shared addresses would make sender-based attribution ambiguous,
        which is the one job the registered wallet exists to do."""
        pool.register_wallet(ALICE, self.W1)
        result = pool.register_wallet(BOB, self.W1)
        self.assertEqual(result["reason"], "address_taken")
        self.assertNotIn("claimed_by", result)  # not Bob's to learn
        self.assertIsNone(pool.get_wallet(BOB))

    def test_a_deposit_from_the_address_is_what_verifies_it(self) -> None:
        pool.register_wallet(ALICE, self.W1)
        req = pool.request_deposit(ALICE, 600.0, txid=txhash("a"))
        pool.decide_deposit(
            req["request_id"], admin_id=ADMIN, approve=True, sender=self.W1
        )
        self.assertEqual(pool.get_wallet(ALICE)["status"], "verified")
        target = pool.payout_target(ALICE)
        self.assertTrue(target["ok"])
        self.assertEqual(target["address"], self.W1)

    def test_a_deposit_from_elsewhere_credits_but_proves_nothing(self) -> None:
        """The money is in the account either way — it arrived. What it does
        not do is establish that the registered address is really theirs."""
        pool.register_wallet(ALICE, self.W1)
        req = pool.request_deposit(ALICE, 600.0, txid=txhash("b"))
        result = pool.decide_deposit(
            req["request_id"], admin_id=ADMIN, approve=True, sender=self.W2
        )
        self.assertEqual(result["status"], "credited")
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 600.0)
        self.assertFalse(result["wallet_verified"])
        self.assertEqual(pool.get_wallet(ALICE)["status"], "pending")
        self.assertEqual(pool.payout_target(ALICE)["reason"], "unverified")

    def test_crediting_without_a_sender_never_fakes_the_proof(self) -> None:
        """Tapping Credit means an admin saw funds arrive, not that they saw
        where from. The flag must not be inferred from the tap."""
        pool.register_wallet(ALICE, self.W1)
        req = pool.request_deposit(ALICE, 600.0, txid=txhash("c"))
        result = pool.decide_deposit(req["request_id"], admin_id=ADMIN, approve=True)
        self.assertEqual(result["status"], "credited")
        self.assertFalse(result["wallet_verified"])
        self.assertEqual(pool.get_wallet(ALICE)["status"], "pending")

    def test_a_deposit_needs_a_registered_wallet_first(self) -> None:
        self.assertEqual(
            pool.request_deposit(ALICE, 600.0, txid=txhash("d"))["reason"],
            "wallet_required",
        )

    def test_changing_the_address_is_not_self_serve(self) -> None:
        """A hijacked Telegram account's first move is to re-point the payout
        address, so the tester alone cannot complete this."""
        pool.register_wallet(ALICE, self.W1)
        result = pool.register_wallet(ALICE, self.W2)
        self.assertEqual(result["reason"], "change_needs_admin")
        self.assertEqual(pool.get_wallet(ALICE)["address"], self.W1)

    def test_an_approved_change_swaps_the_address_and_holds_payouts(self) -> None:
        pool.register_wallet(ALICE, self.W1)
        req = pool.request_wallet_change(ALICE, self.W2)
        self.assertTrue(req["ok"], req)
        # The old address still answers until an admin rules.
        self.assertEqual(pool.get_wallet(ALICE)["address"], self.W1)

        decided = pool.decide_wallet_change(
            req["request_id"], admin_id=ADMIN, approve=True
        )
        self.assertTrue(decided["approved"])
        wallet = pool.get_wallet(ALICE)
        self.assertEqual(wallet["address"], self.W2)
        # Unproven again, and held: a new address has shown us nothing yet.
        self.assertEqual(wallet["status"], "pending")
        self.assertEqual(pool.payout_target(ALICE)["reason"], "unverified")

    def test_the_cooldown_outlives_verification(self) -> None:
        """The hold is the window in which a victim can still object, so
        proving the new address must not cut it short."""
        pool.register_wallet(ALICE, self.W1)
        req = pool.request_wallet_change(ALICE, self.W2)
        pool.decide_wallet_change(req["request_id"], admin_id=ADMIN, approve=True)
        pool.mark_wallet_verified(self.W2, txid=txhash("e"))

        target = pool.payout_target(ALICE)
        self.assertFalse(target["ok"])
        self.assertEqual(target["reason"], "cooldown")
        self.assertGreater(target["until"], pool._now())

    def test_a_rejected_change_leaves_the_old_address_alone(self) -> None:
        pool.register_wallet(ALICE, self.W1)
        req = pool.request_wallet_change(ALICE, self.W2)
        pool.decide_wallet_change(req["request_id"], admin_id=ADMIN, approve=False)
        self.assertEqual(pool.get_wallet(ALICE)["address"], self.W1)
        self.assertIsNone(pool.get_wallet_change_request(ALICE))

    def test_one_change_request_at_a_time(self) -> None:
        third = "0x3333333333333333333333333333333333333333"
        pool.register_wallet(ALICE, self.W1)
        self.assertTrue(pool.request_wallet_change(ALICE, self.W2)["ok"])
        self.assertEqual(
            pool.request_wallet_change(ALICE, third)["reason"], "already_pending"
        )
        # Naming the address they already have is answered as such, not queued.
        self.assertEqual(
            pool.request_wallet_change(ALICE, self.W1)["reason"], "same_address"
        )

    def test_a_change_cannot_be_decided_twice(self) -> None:
        pool.register_wallet(ALICE, self.W1)
        req = pool.request_wallet_change(ALICE, self.W2)
        pool.decide_wallet_change(req["request_id"], admin_id=ADMIN, approve=True)
        again = pool.decide_wallet_change(
            req["request_id"], admin_id=ADMIN, approve=True
        )
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "already_decided")

    def test_only_one_live_address_survives_a_change(self) -> None:
        """The index behind "where do we pay this person" having one answer."""
        pool.register_wallet(ALICE, self.W1)
        req = pool.request_wallet_change(ALICE, self.W2)
        pool.decide_wallet_change(req["request_id"], admin_id=ADMIN, approve=True)
        with sqlite3.connect(self._db) as conn:
            live = conn.execute(
                "SELECT COUNT(*) FROM pool_wallets WHERE telegram_id = ? AND "
                "status IN ('pending', 'verified')",
                (ALICE,),
            ).fetchone()[0]
        self.assertEqual(live, 1)
        # And the old one is retained as history, not deleted.
        self.assertEqual(pool.wallet_owner(self.W1), None)

    def test_an_unapproved_user_cannot_register(self) -> None:
        self.assertEqual(
            pool.register_wallet(999, self.W1)["reason"], "not_approved"
        )

    def test_verifying_an_unregistered_address_is_a_no_op(self) -> None:
        self.assertEqual(
            pool.mark_wallet_verified(self.W1)["reason"], "not_registered"
        )


class WithdrawalTests(PoolTestCase):
    """The payout lifecycle, and specifically the states where money is lost.

    Coinbase offers no idempotency on sends, so the usual safety net is gone:
    a resend is a second real payment. These pin the three ways that bites —
    paying twice, refunding money that already left, and letting a tester
    withdraw what is committed to an open trade.
    """

    def setUp(self) -> None:
        super().setUp()
        self._patch(bot_config, "POOL_MIN_WITHDRAWAL_USD", 50.0)
        self._patch(bot_config, "POOL_WITHDRAWAL_FEE_RESERVE_USD", 3.0)
        self._patch(bot_config, "POOL_MAX_WITHDRAWAL_USD", 2500.0)
        self._patch(bot_config, "POOL_MAX_USER_DAILY_WITHDRAWAL_USD", 2500.0)
        self._patch(bot_config, "POOL_MAX_GLOBAL_DAILY_WITHDRAWAL_USD", 5000.0)
        self._patch(bot_config, "POOL_PAYOUTS_ENABLED", True)
        # Most of this class exercises the admin-gated lifecycle (approve,
        # reject, refund), which only exists on a `requested` row. The
        # auto-approval cases opt back in explicitly.
        self._patch(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", False)
        pool.approve_user(ALICE, admin_id=ADMIN)
        self.alice_wallet = self._wallet(ALICE)
        pool.mark_wallet_verified(self.alice_wallet)
        pool.credit(ALICE, 1000.0, admin_id=ADMIN, ref="seed")

    def _patch(self, target, attr, value) -> None:
        p = patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def test_auto_approved_payouts_still_obey_every_limit(self) -> None:
        """Auto-approval removes a human step, not a check.

        The admin tap never decided anything — halt, caps, destination and
        balance all resolve at request time. If any of them stopped enforcing
        when the human left, this is where a tester's money walks out.
        """
        self._patch(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", True)
        cases = {
            "below_minimum": lambda: pool.request_withdrawal(ALICE, 5.0),
            "above_max": lambda: pool.request_withdrawal(ALICE, 99_000.0),
            # Under the per-request cap, over the balance.
            "insufficient_available": lambda: pool.request_withdrawal(ALICE, 1200.0),
        }
        for reason, call in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(call().get("reason"), reason)

        pool.halt_payouts("testing")
        self.assertEqual(pool.request_withdrawal(ALICE, 100.0)["reason"], "halted")
        pool.resume_payouts()

        # An unverified destination is still refused: return-to-source is the
        # guard that auto-approval leans on hardest.
        pool.approve_user(BOB, admin_id=ADMIN)
        pool.credit(BOB, 1000.0, admin_id=ADMIN, ref="seed-bob")
        self._wallet(BOB)  # registered but never proven on-chain
        self.assertFalse(pool.request_withdrawal(BOB, 100.0)["ok"])

    def test_auto_approval_sends_without_an_admin(self) -> None:
        self._patch(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", True)
        result = pool.request_withdrawal(ALICE, 100.0)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["auto_approved"])
        # The payout sweep picks up `approved`, so this goes out on the next
        # 60s pass with nobody in the loop.
        queued = [w["id"] for w in pool.pending_withdrawals("approved")]
        self.assertIn(int(result["withdrawal_id"]), queued)
        row = pool.get_withdrawal(int(result["withdrawal_id"]))
        self.assertEqual(row["status"], "approved")
        self.assertIsNotNone(row["approved_at"])
        self.assertIsNone(row["approved_by"])  # no human claimed it

    def test_the_flag_off_restores_the_admin_gate(self) -> None:
        self._patch(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", False)
        result = pool.request_withdrawal(ALICE, 100.0)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result.get("auto_approved"))
        self.assertEqual(pool.pending_withdrawals("approved"), [])
        wid = int(result["withdrawal_id"])
        self.assertEqual(pool.get_withdrawal(wid)["status"], "requested")
        self.assertTrue(
            pool.decide_withdrawal(wid, admin_id=ADMIN, approve=True)["ok"]
        )

    def test_the_debit_happens_at_request_time(self) -> None:
        """Money is taken when the request is made, not when it is sent.
        Otherwise a tester can queue two withdrawals against one balance."""
        result = pool.request_withdrawal(ALICE, 500.0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["debited_usd"], 503.0)   # amount + reserve
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 497.0)

    def test_two_requests_cannot_spend_one_balance(self) -> None:
        self.assertTrue(pool.request_withdrawal(ALICE, 600.0)["ok"])
        second = pool.request_withdrawal(ALICE, 600.0)
        self.assertEqual(second["reason"], "insufficient_available")
        self.assertGreaterEqual(float(pool.get_account(ALICE)["cash_usd"]), 0.0)

    def test_money_in_an_open_trade_cannot_be_withdrawn(self) -> None:
        pool.record_intent("ref-1", ALICE)
        reserved = float(pool.get_account(ALICE)["reserved_usd"])
        self.assertGreater(reserved, 0)
        result = pool.request_withdrawal(ALICE, 1000.0)
        self.assertEqual(result["reason"], "insufficient_available")

    def test_the_quoted_max_actually_clears(self) -> None:
        quoted = pool.max_withdrawal_usd(ALICE)
        self.assertTrue(pool.request_withdrawal(ALICE, quoted)["ok"])

    def test_the_quoted_max_still_clears_after_an_earlier_withdrawal(self) -> None:
        """The daily caps count what already went out, so the quote must too —
        otherwise "/withdraw all" asks for a number the very next check
        refuses."""
        self._patch(bot_config, "POOL_MAX_USER_DAILY_WITHDRAWAL_USD", 600.0)
        self.assertTrue(pool.request_withdrawal(ALICE, 300.0)["ok"])
        quoted = pool.max_withdrawal_usd(ALICE)
        # $600 cap, $303 already out today, $3 reserve on the next send.
        self.assertAlmostEqual(quoted, 294.0, places=2)
        self.assertTrue(pool.request_withdrawal(ALICE, quoted)["ok"])

    def test_the_max_leaves_room_for_the_fee(self) -> None:
        """Quoting the raw balance would fail at the moment someone tries to
        take their money out, because the fee rides on top of the send."""
        self.assertEqual(
            pool.max_withdrawal_usd(ALICE),
            round(pool.withdrawable_usd(ALICE) - 3.0, 2),
        )

    def test_below_the_minimum_is_refused(self) -> None:
        result = pool.request_withdrawal(ALICE, 49.99)
        self.assertEqual(result["reason"], "below_minimum")
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)

    def test_an_unverified_wallet_cannot_be_paid(self) -> None:
        pool.approve_user(BOB, admin_id=ADMIN)
        self._wallet(BOB)                      # registered, never verified
        pool.credit(BOB, 1000.0, admin_id=ADMIN, ref="seed-bob")
        result = pool.request_withdrawal(BOB, 100.0)
        self.assertEqual(result["reason"], "unverified")
        self.assertEqual(float(pool.get_account(BOB)["cash_usd"]), 1000.0)

    def test_rejecting_refunds_in_full(self) -> None:
        req = pool.request_withdrawal(ALICE, 500.0)
        pool.decide_withdrawal(req["withdrawal_id"], admin_id=ADMIN, approve=False)
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)

    def test_a_failed_send_refunds_in_full(self) -> None:
        req = pool.request_withdrawal(ALICE, 500.0)
        wid = req["withdrawal_id"]
        pool.decide_withdrawal(wid, admin_id=ADMIN, approve=True)
        pool.mark_withdrawal_submitting(wid)
        pool.mark_withdrawal_failed(wid, reason="venue refused")
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)

    def test_a_failed_send_refunds_only_once(self) -> None:
        req = pool.request_withdrawal(ALICE, 500.0)
        wid = req["withdrawal_id"]
        pool.decide_withdrawal(wid, admin_id=ADMIN, approve=True)
        pool.mark_withdrawal_failed(wid, reason="one")
        pool.mark_withdrawal_failed(wid, reason="two")
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)

    def test_an_unknown_outcome_neither_refunds_nor_retries(self) -> None:
        """Refunding could hand back money that already left; retrying could
        send it twice. Coinbase will not say which, so it halts."""
        req = pool.request_withdrawal(ALICE, 500.0)
        wid = req["withdrawal_id"]
        pool.decide_withdrawal(wid, admin_id=ADMIN, approve=True)
        pool.mark_withdrawal_submitting(wid)
        pool.mark_withdrawal_unknown(wid, reason="timeout after send")

        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 497.0)
        self.assertEqual(pool.get_withdrawal(wid)["status"], "unknown")
        self.assertIsNotNone(pool.payouts_halted())
        self.assertEqual(pool.pending_withdrawals("approved"), [])

    def test_a_halt_blocks_new_requests_until_cleared(self) -> None:
        pool.halt_payouts("something ambiguous")
        self.assertEqual(pool.request_withdrawal(ALICE, 100.0)["reason"], "halted")
        pool.resume_payouts()
        self.assertTrue(pool.request_withdrawal(ALICE, 100.0)["ok"])

    def test_only_an_approved_payout_can_be_claimed_for_sending(self) -> None:
        """The guard that keeps two sweeps from sending the same payout."""
        req = pool.request_withdrawal(ALICE, 500.0)
        wid = req["withdrawal_id"]
        self.assertFalse(pool.mark_withdrawal_submitting(wid))  # not approved
        pool.decide_withdrawal(wid, admin_id=ADMIN, approve=True)
        self.assertTrue(pool.mark_withdrawal_submitting(wid))
        self.assertFalse(pool.mark_withdrawal_submitting(wid))  # already claimed

    def test_the_fee_reserve_is_trued_up_to_the_real_fee(self) -> None:
        """The reserve is headroom for a gas spike, not a charge."""
        req = pool.request_withdrawal(ALICE, 500.0)
        wid = req["withdrawal_id"]
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 497.0)

        pool.decide_withdrawal(wid, admin_id=ADMIN, approve=True)
        pool.mark_withdrawal_submitting(wid)
        out = pool.mark_withdrawal_submitted(wid, cb_tx_id="cb1", fee_usd=0.148356)

        self.assertAlmostEqual(out["refunded_usd"], 2.85, places=2)
        self.assertAlmostEqual(
            float(pool.get_account(ALICE)["cash_usd"]), 499.85, places=2
        )
        row = pool.get_withdrawal(wid)
        self.assertAlmostEqual(row["debited_usd"], 500.15, places=2)

    def test_the_tester_pays_the_fee_so_books_match_the_venue(self) -> None:
        """They receive what they asked for; their balance drops by more."""
        req = pool.request_withdrawal(ALICE, 500.0)
        wid = req["withdrawal_id"]
        pool.decide_withdrawal(wid, admin_id=ADMIN, approve=True)
        pool.mark_withdrawal_submitting(wid)
        pool.mark_withdrawal_submitted(wid, cb_tx_id="cb1", fee_usd=0.15)

        row = pool.get_withdrawal(wid)
        self.assertEqual(row["amount_usd"], 500.0)          # they receive this
        self.assertAlmostEqual(row["debited_usd"], 500.15)  # ledger lost this
        self.assertAlmostEqual(
            1000.0 - float(pool.get_account(ALICE)["cash_usd"]), 500.15, places=2
        )

    def test_the_address_is_frozen_at_request_time(self) -> None:
        """Changing the payout wallet must not redirect a payout already in
        flight — that is what a takeover would try."""
        req = pool.request_withdrawal(ALICE, 500.0)
        other = "0x" + "cd" * 20
        pool.request_wallet_change(ALICE, other)
        pool.decide_wallet_change(ALICE, admin_id=ADMIN, approve=True)
        self.assertEqual(pool.get_withdrawal(req["withdrawal_id"])["to_address"],
                         self.alice_wallet.lower())

    def test_caps_bound_the_daily_total_not_just_one_request(self) -> None:
        self._patch(bot_config, "POOL_MAX_USER_DAILY_WITHDRAWAL_USD", 600.0)
        self.assertTrue(pool.request_withdrawal(ALICE, 500.0)["ok"])
        second = pool.request_withdrawal(ALICE, 200.0)
        self.assertEqual(second["reason"], "user_daily_cap")

    def test_a_rejected_payout_does_not_count_against_the_cap(self) -> None:
        self._patch(bot_config, "POOL_MAX_USER_DAILY_WITHDRAWAL_USD", 600.0)
        req = pool.request_withdrawal(ALICE, 500.0)
        pool.decide_withdrawal(req["withdrawal_id"], admin_id=ADMIN, approve=False)
        self.assertTrue(pool.request_withdrawal(ALICE, 500.0)["ok"])

    def test_the_global_cap_bounds_everyone_together(self) -> None:
        self._patch(bot_config, "POOL_MAX_GLOBAL_DAILY_WITHDRAWAL_USD", 600.0)
        pool.approve_user(BOB, admin_id=ADMIN)
        pool.mark_wallet_verified(self._wallet(BOB))
        pool.credit(BOB, 1000.0, admin_id=ADMIN, ref="seed-bob")

        self.assertTrue(pool.request_withdrawal(ALICE, 500.0)["ok"])
        self.assertEqual(
            pool.request_withdrawal(BOB, 500.0)["reason"], "global_daily_cap"
        )

    def test_concurrent_requests_cannot_overdraw(self) -> None:
        import threading

        barrier = threading.Barrier(6)
        out: list = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            r = pool.request_withdrawal(ALICE, 500.0)
            with lock:
                out.append(r)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        winners = [r for r in out if r.get("ok")]
        self.assertEqual(len(winners), 1, out)
        self.assertGreaterEqual(float(pool.get_account(ALICE)["cash_usd"]), 0.0)


class LedgerRaceTests(PoolTestCase):
    """Concurrency, run concurrently.

    These use real threads against a real file-backed ledger because the bug
    they pin is invisible to sequential calls: every one of these assertions
    passes with the guard removed, so long as the calls are made one at a
    time. That is why the defect could sit in a money path unnoticed.
    """

    @staticmethod
    def _race(n: int, fn) -> list:
        """Run fn(i) on n threads, released together to maximise overlap."""
        barrier = threading.Barrier(n)
        out: list = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            barrier.wait()
            result = fn(i)
            with lock:
                out.append(result)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return out

    def test_simultaneous_withdrawals_cannot_overdraw(self) -> None:
        """Eight requests for the whole balance, at once. Exactly one may win.

        Refs are deliberately distinct, so the dedupe index cannot be what
        saves us — the availability check has to, and it only can if the read
        and the write are one step.
        """
        self._fund(ALICE, 1000.0)
        results = self._race(
            8,
            lambda i: pool.debit(ALICE, 1000.0, admin_id=ADMIN, ref=f"w:{i}"),
        )
        winners = [r for r in results if r.get("ok")]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 0.0)
        self.assertGreaterEqual(float(pool.get_account(ALICE)["cash_usd"]), 0.0)

    def test_partial_withdrawals_racing_stay_within_the_balance(self) -> None:
        """Ten requests for $150 against $1,000: six can be paid, not ten."""
        self._fund(ALICE, 1000.0)
        results = self._race(
            10,
            lambda i: pool.debit(ALICE, 150.0, admin_id=ADMIN, ref=f"p:{i}"),
        )
        paid = sum(150.0 for r in results if r.get("ok"))
        cash = float(pool.get_account(ALICE)["cash_usd"])
        self.assertEqual(round(cash + paid, 2), 1000.0)
        self.assertGreaterEqual(cash, 0.0)
        self.assertLessEqual(paid, 1000.0)

    def test_simultaneous_credits_do_not_lose_one(self) -> None:
        """_apply_event reads a balance, adds in Python, writes the absolute
        result. Concurrently, the second write silently erases the first —
        and the journal still looks plausible afterwards."""
        pool.approve_user(ALICE, admin_id=ADMIN)
        self._race(
            10,
            lambda i: pool.credit(ALICE, 100.0, admin_id=ADMIN, ref=f"c:{i}"),
        )
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)
        self.assertEqual(pool.total_tester_cash(), 1000.0)

    def test_simultaneous_accepts_each_size_off_fresh_cash(self) -> None:
        """Five Accepts at once must not all budget against the same dollars.

        Serialized, each sees the previous reserve, so the five budgets are
        strictly distinct. Identical budgets are the signature of every thread
        reading the pre-reserve balance.
        """
        self._fund(ALICE, 1000.0)
        results = self._race(
            5, lambda i: pool.record_intent(f"ref-{i}", ALICE)
        )
        risks = [r["risk_usd"] for r in results if r.get("ok")]
        self.assertEqual(len(risks), 5, results)
        self.assertEqual(len(set(risks)), 5, f"all sized off stale cash: {risks}")

        account = pool.get_account(ALICE)
        self.assertEqual(round(float(account["reserved_usd"]), 2),
                         round(sum(risks), 2))
        self.assertLessEqual(float(account["reserved_usd"]),
                             float(account["cash_usd"]))

    def test_the_same_withdrawal_retried_books_once(self) -> None:
        """A payout retried after a timeout must not charge twice. This is
        what the ref is for: the index only covers non-NULL refs, so the old
        ref=None withdrawal could be replayed freely."""
        self._fund(ALICE, 1000.0)
        first = pool.debit(ALICE, 200.0, admin_id=ADMIN,
                           ref="withdrawal_request:7")
        again = pool.debit(ALICE, 200.0, admin_id=ADMIN,
                           ref="withdrawal_request:7")
        self.assertTrue(first["ok"], first)
        self.assertEqual(again.get("reason"), "duplicate")
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 800.0)

    def test_the_same_withdrawal_retried_concurrently_books_once(self) -> None:
        self._fund(ALICE, 1000.0)
        results = self._race(
            6,
            lambda i: pool.debit(ALICE, 200.0, admin_id=ADMIN,
                                 ref="withdrawal_request:9"),
        )
        self.assertEqual(len([r for r in results if r.get("ok")]), 1, results)
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 800.0)

    def test_two_intended_adhoc_debits_both_book(self) -> None:
        """The flip side: an admin correction has no operation id and can
        legitimately repeat, so it must not be deduped into silence."""
        self._fund(ALICE, 1000.0)
        first = pool.debit(ALICE, 500.0, admin_id=ADMIN)
        second = pool.debit(ALICE, 500.0, admin_id=ADMIN)
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(first["ref"], second["ref"])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 0.0)

    def test_every_balance_event_carries_a_ref(self) -> None:
        """A NULL ref is outside the dedupe index, so it is a replay waiting
        to happen. Nothing that moves money should leave one behind."""
        self._fund(ALICE, 1000.0)
        pool.record_intent("ref-x", ALICE)
        pool.release_intents("ref-x")
        pool.debit(ALICE, 50.0, admin_id=ADMIN)
        conn = sqlite3.connect(self._db)
        conn.row_factory = sqlite3.Row
        orphans = conn.execute(
            "SELECT kind, amount_usd FROM pool_events WHERE ref IS NULL"
        ).fetchall()
        conn.close()
        self.assertEqual([dict(r) for r in orphans], [])


class WithdrawableTests(PoolTestCase):
    def test_withdrawable_excludes_money_committed_to_a_trade(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.record_intent("ref-1", ALICE)
        reserved = float(pool.get_account(ALICE)["reserved_usd"])
        self.assertGreater(reserved, 0)
        self.assertEqual(pool.withdrawable_usd(ALICE), round(1000.0 - reserved, 2))

    def test_a_withdrawal_cannot_reach_reserved_margin(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.record_intent("ref-1", ALICE)
        result = pool.debit(ALICE, 1000.0, admin_id=ADMIN, ref="w:1")
        self.assertEqual(result["reason"], "insufficient_available")
        self.assertEqual(result["available_usd"], pool.withdrawable_usd(ALICE))

    def test_the_quoted_max_is_exactly_what_clears(self) -> None:
        """Whatever "withdraw max" shows has to be accepted, or the button
        quotes a number the ledger then refuses."""
        self._fund(ALICE, 1000.0)
        pool.record_intent("ref-1", ALICE)
        quoted = pool.withdrawable_usd(ALICE)
        self.assertTrue(
            pool.debit(ALICE, quoted, admin_id=ADMIN, ref="w:max")["ok"]
        )
        self.assertEqual(pool.withdrawable_usd(ALICE), 0.0)

    def test_withdrawable_never_goes_negative(self) -> None:
        self._fund(ALICE, 1000.0)
        self.assertEqual(pool.withdrawable_usd(BOB), 0.0)  # no account at all

    def test_an_unfunded_account_can_withdraw_nothing(self) -> None:
        pool.approve_user(ALICE, admin_id=ADMIN)
        self.assertEqual(pool.withdrawable_usd(ALICE), 0.0)
        self.assertFalse(pool.debit(ALICE, 10.0, admin_id=ADMIN, ref="w")["ok"])


class AutoCreditTests(PoolTestCase):
    """Crediting a deposit with no human in the loop.

    Coinbase does not report a sender for an incoming transfer, so attribution
    is by transaction hash and nothing else. These tests pin the three ways
    that could go wrong: paying twice, paying the wrong person, and paying on
    an amount the tester chose rather than the one that arrived.
    """

    def setUp(self) -> None:
        super().setUp()
        pool.approve_user(ALICE, admin_id=ADMIN)
        pool.approve_user(BOB, admin_id=ADMIN)
        self._wallet(ALICE)
        self._wallet(BOB)
        # Past the baseline, so tests exercise the live path. A first sweep
        # with no transfers establishes it without side effects.
        pool.observe_chain_deposits([])

    @staticmethod
    def _transfer(cb_id: str, amount: float, txid: str | None) -> dict:
        return {"id": cb_id, "amount": amount, "currency": "USDC",
                "txid": txid, "network": "ethereum",
                "created_at": "2026-09-15T20:00:00Z"}

    def test_a_claimed_transfer_credits_itself(self) -> None:
        h = txhash("a")
        req = pool.request_deposit(ALICE, 1000.0, txid=h)
        events = pool.observe_chain_deposits([self._transfer("cb1", 1000.0, h)])

        self.assertEqual([e["kind"] for e in events], ["credited"])
        self.assertEqual(events[0]["telegram_id"], ALICE)
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)
        # The claim is closed, so the admin card cannot pay it a second time.
        self.assertEqual(
            pool.get_deposit_request(req["request_id"])["status"], "credited"
        )
        self.assertFalse(
            pool.decide_deposit(req["request_id"], admin_id=ADMIN, approve=True)["ok"]
        )
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)

    def test_re_running_the_sweep_pays_once(self) -> None:
        """The sweep runs every 60s over a list that still contains old rows,
        so idempotence is not an edge case — it is the normal path."""
        h = txhash("b")
        pool.request_deposit(ALICE, 1000.0, txid=h)
        transfers = [self._transfer("cb2", 1000.0, h)]
        for _ in range(5):
            pool.observe_chain_deposits(transfers)
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)

    def test_the_venue_amount_wins_over_the_claim(self) -> None:
        """A tester must not be able to move their own balance by typing a
        bigger number than they sent."""
        h = txhash("c")
        pool.request_deposit(ALICE, 5000.0, txid=h)   # claims $5,000
        events = pool.observe_chain_deposits([self._transfer("cb3", 900.0, h)])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 900.0)
        self.assertTrue(events[0]["mismatch"])
        self.assertEqual(events[0]["claimed_usd"], 5000.0)

    def test_a_hash_claimed_by_someone_else_cannot_be_hijacked(self) -> None:
        h = txhash("d")
        pool.request_deposit(ALICE, 1000.0, txid=h)
        # Bob cannot even file it; the txid index refuses him first.
        self.assertEqual(
            pool.request_deposit(BOB, 1000.0, txid=h)["reason"],
            "txid_already_claimed",
        )
        pool.observe_chain_deposits([self._transfer("cb4", 1000.0, h)])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)
        self.assertEqual(float(pool.get_account(BOB)["cash_usd"]), 0.0)

    def test_an_unclaimed_transfer_is_flagged_never_apportioned(self) -> None:
        """Guessing an owner from an amount is how one tester is credited with
        another's money."""
        events = pool.observe_chain_deposits(
            [self._transfer("cb5", 2500.0, txhash("e"))]
        )
        self.assertEqual([e["kind"] for e in events], ["unmatched"])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 0.0)
        self.assertEqual(float(pool.get_account(BOB)["cash_usd"]), 0.0)
        self.assertEqual(pool.total_tester_cash(), 0.0)
        self.assertEqual(len(pool.unmatched_chain_deposits()), 1)

    def test_an_unclaimed_transfer_is_raised_once_not_every_minute(self) -> None:
        pool.observe_chain_deposits([self._transfer("cb6", 100.0, txhash("f"))])
        pool.mark_chain_deposit_alerted("cb6")
        again = pool.observe_chain_deposits(
            [self._transfer("cb6", 100.0, txhash("f"))]
        )
        self.assertTrue(again[0]["alerted"])

    def test_an_orphan_can_be_assigned_and_only_pays_once(self) -> None:
        pool.observe_chain_deposits([self._transfer("cb7", 750.0, txhash("1"))])
        result = pool.assign_chain_deposit("cb7", ALICE, admin_id=ADMIN)
        self.assertTrue(result["ok"], result)
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 750.0)

        twice = pool.assign_chain_deposit("cb7", BOB, admin_id=ADMIN)
        self.assertEqual(twice["reason"], "already_credited")
        self.assertEqual(float(pool.get_account(BOB)["cash_usd"]), 0.0)
        # And the sweep must not re-credit an assigned transfer either.
        pool.observe_chain_deposits([self._transfer("cb7", 750.0, txhash("1"))])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 750.0)

    def test_a_manual_credit_then_arrival_does_not_double_pay(self) -> None:
        """An impatient admin taps Credit, then the transfer lands. The ledger
        must not book it twice, and nobody should be alerted about an orphan."""
        h = txhash("2")
        req = pool.request_deposit(ALICE, 600.0, txid=h)
        pool.decide_deposit(req["request_id"], admin_id=ADMIN, approve=True)
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 600.0)

        events = pool.observe_chain_deposits([self._transfer("cb8", 600.0, h)])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 600.0)
        self.assertEqual(events, [])

    def test_outbound_and_unsettled_transfers_are_ignored(self) -> None:
        h = txhash("3")
        pool.request_deposit(ALICE, 600.0, txid=h)
        # A negative amount is money leaving; the gateway filters unsettled
        # rows, so a zero/negative here stands for anything not an arrival.
        pool.observe_chain_deposits([self._transfer("cb9", -600.0, h)])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 0.0)

    def test_a_transfer_with_no_hash_is_never_guessed_at(self) -> None:
        pool.request_deposit(ALICE, 600.0, txid=txhash("4"))
        events = pool.observe_chain_deposits([self._transfer("cb10", 600.0, None)])
        self.assertEqual([e["kind"] for e in events], ["unmatched"])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 0.0)

    def test_auto_credit_does_not_claim_to_have_verified_the_wallet(self) -> None:
        """Coinbase reports no sender, so arrival proves the money is real but
        not who sent it. Withdrawals depend on that distinction."""
        h = txhash("5")
        pool.request_deposit(ALICE, 1000.0, txid=h)
        pool.observe_chain_deposits([self._transfer("cb11", 1000.0, h)])
        self.assertEqual(pool.get_wallet(ALICE)["status"], "pending")
        self.assertEqual(pool.payout_target(ALICE)["reason"], "unverified")


class ChainBaselineTests(PoolTestCase):
    def test_the_first_sweep_does_not_touch_pre_existing_transfers(self) -> None:
        """Two house deposits ($3,000 and $1,000) already sit on this address.
        A watcher that treated history as unclaimed tester money would alert on
        both, and any amount-matching would hand them to whoever asked."""
        pool.approve_user(ALICE, admin_id=ADMIN)
        history = [
            {"id": "old1", "amount": 3000.0, "currency": "USDC",
             "txid": txhash("9"), "network": "ethereum", "created_at": ""},
            {"id": "old2", "amount": 1000.0, "currency": "USDC",
             "txid": txhash("8"), "network": "ethereum", "created_at": ""},
        ]
        events = pool.observe_chain_deposits(history)
        self.assertEqual(events, [])
        self.assertEqual(pool.unmatched_chain_deposits(), [])
        self.assertEqual(pool.total_tester_cash(), 0.0)

    def test_a_claim_beats_the_baseline_on_the_very_first_sweep(self) -> None:
        """A deposit landing during the first sweep must still be credited,
        not buried as history."""
        pool.approve_user(ALICE, admin_id=ADMIN)
        self._wallet(ALICE)
        h = txhash("7")
        pool.request_deposit(ALICE, 800.0, txid=h)
        events = pool.observe_chain_deposits([
            {"id": "old3", "amount": 3000.0, "currency": "USDC",
             "txid": txhash("6"), "network": "ethereum", "created_at": ""},
            {"id": "new1", "amount": 800.0, "currency": "USDC",
             "txid": h, "network": "ethereum", "created_at": ""},
        ])
        self.assertEqual([e["kind"] for e in events], ["credited"])
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 800.0)

    def test_history_is_only_baselined_once(self) -> None:
        pool.observe_chain_deposits([])
        events = pool.observe_chain_deposits([
            {"id": "later", "amount": 500.0, "currency": "USDC",
             "txid": txhash("5"), "network": "ethereum", "created_at": ""},
        ])
        self.assertEqual([e["kind"] for e in events], ["unmatched"])


class DepositRequestTests(PoolTestCase):
    def setUp(self) -> None:
        super().setUp()
        pool.approve_user(ALICE, admin_id=ADMIN)
        self._wallet(ALICE)

    def test_request_and_one_tap_credit(self) -> None:
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
        self.assertEqual(
            pool.request_deposit(ALICE, 100.0, txid=txhash("a"))["reason"],
            "below_minimum",
        )
        first = pool.request_deposit(ALICE, 600.0, txid=txhash("b"))
        self.assertTrue(first["ok"])
        second = pool.request_deposit(ALICE, 700.0, txid=txhash("c"))
        self.assertEqual(second["reason"], "already_pending")

    def test_denied_request_moves_no_money(self) -> None:
        req = pool.request_deposit(ALICE, 600.0, txid=txhash("d"))
        result = pool.decide_deposit(req["request_id"], admin_id=ADMIN, approve=False)
        self.assertEqual(result["status"], "denied")
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 0.0)


class ResetTestUserTests(PoolTestCase):
    """The demo-reset tool deletes money rows, so its refusal is pinned here.

    It exists to make an onboarding recording repeatable. The failure it must
    never allow is being pointed at a real tester and quietly wiping their
    balance, so the guard is tested rather than trusted.
    """

    def setUp(self) -> None:
        super().setUp()
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
        import _reset_test_user

        self.tool = _reset_test_user
        pool.approve_user(ALICE, admin_id=ADMIN)
        self._wallet(ALICE)

    def test_a_fresh_id_has_nothing_to_reset(self) -> None:
        result = self.tool.reset(-12345, confirm=True, force=False)
        self.assertEqual(result["action"], "nothing")

    def test_an_account_holding_money_is_refused(self) -> None:
        pool.credit(ALICE, 600.0, admin_id=ADMIN, ref="reset-test")
        result = self.tool.reset(ALICE, confirm=True, force=False)
        self.assertEqual(result["action"], "refused")
        # And nothing was touched — a refusal that half-deleted would be worse
        # than no guard at all.
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 600.0)
        self.assertIsNotNone(pool.get_wallet(ALICE))

    def test_reserved_money_also_refuses(self) -> None:
        """Cash can read as zero while the balance is committed to a trade."""
        pool.credit(ALICE, 600.0, admin_id=ADMIN, ref="reset-test")
        pool.record_intent("ref-1", ALICE)
        pool.debit(ALICE, float(pool.get_account(ALICE)["cash_usd"]),
                   admin_id=ADMIN, ref="drain")
        account = pool.get_account(ALICE)
        self.assertGreater(float(account["reserved_usd"]), 0.0)
        self.assertEqual(
            self.tool.reset(ALICE, confirm=True, force=False)["action"],
            "refused",
        )

    def test_a_dry_run_deletes_nothing(self) -> None:
        result = self.tool.reset(ALICE, confirm=False, force=False)
        self.assertEqual(result["action"], "dry_run")
        self.assertTrue(pool.is_approved(ALICE))
        self.assertIsNotNone(pool.get_wallet(ALICE))

    def test_an_empty_account_resets_to_first_contact(self) -> None:
        self.assertEqual(
            self.tool.reset(ALICE, confirm=True, force=False)["action"], "reset"
        )
        self.assertFalse(pool.is_approved(ALICE))
        self.assertIsNone(pool.get_wallet(ALICE))
        self.assertIsNone(pool.get_account(ALICE))
        # Which is the point: /start shows the review message again.
        self.assertEqual(pool.request_access(ALICE, "alice"), "new")

    def test_only_the_named_id_is_touched(self) -> None:
        pool.approve_user(BOB, admin_id=ADMIN)
        self._wallet(BOB)
        pool.credit(BOB, 900.0, admin_id=ADMIN, ref="bob-untouched")
        self.tool.reset(ALICE, confirm=True, force=False)
        self.assertEqual(float(pool.get_account(BOB)["cash_usd"]), 900.0)
        self.assertTrue(pool.is_approved(BOB))
        self.assertIsNotNone(pool.get_wallet(BOB))


class WalletVerifyTests(PoolTestCase):
    """Proving a wallet against the chain, which is what unlocks payouts.

    Coinbase reports that a deposit arrived but never who sent it, so the
    sender on the transfer is the only evidence a tester controls the address
    they gave us. Two failures are being guarded here and they pull in
    opposite directions: verifying a wallet nobody proved would let client
    funds go to an address its owner never held, while refusing one because
    Etherscan was down would lock an honest tester out of their own money.
    """

    def setUp(self) -> None:
        super().setUp()
        pool.approve_user(ALICE, admin_id=ADMIN)
        self.wallet = self._wallet(ALICE)
        self.txid = txhash("f")
        req = pool.request_deposit(ALICE, 600.0, txid=self.txid)
        pool.decide_deposit(req["request_id"], admin_id=ADMIN, approve=True)

    def _verify(self, result: dict) -> list[dict]:
        return pool.verify_wallets_onchain(
            lambda txid, to_address: result, deposit_address=ADDRESS
        )

    def test_sender_matching_the_registered_wallet_verifies_it(self) -> None:
        events = self._verify(
            {"ok": True, "sender": self.wallet, "amount_usd": 600.0}
        )
        self.assertEqual([e["kind"] for e in events], ["verified"])
        self.assertEqual(pool.get_wallet(ALICE)["status"], "verified")
        self.assertTrue(pool.payout_target(ALICE)["ok"])

    def test_a_different_sender_leaves_the_wallet_unproven(self) -> None:
        """The attack: register an address you control, fund from anywhere
        else, and have the pool pay you at an address it never saw send."""
        events = self._verify(
            {"ok": True, "sender": "0x" + "9" * 40, "amount_usd": 600.0}
        )
        self.assertEqual([e["kind"] for e in events], ["mismatch"])
        self.assertEqual(pool.get_wallet(ALICE)["status"], "pending")
        self.assertEqual(pool.payout_target(ALICE)["reason"], "unverified")

    def test_a_lookup_failure_is_not_a_failed_proof(self) -> None:
        """An Etherscan outage must not read as "it was not them" — that
        would refuse a withdrawal on evidence we never actually gathered."""
        events = self._verify({"ok": False, "reason": "lookup_failed"})
        self.assertEqual(events, [])
        self.assertEqual(pool.get_wallet(ALICE)["status"], "pending")
        # And the proof is still queued, so it is retried rather than dropped.
        self.assertEqual(len(pool.wallet_proofs_to_check()), 1)

        events = self._verify(
            {"ok": True, "sender": self.wallet, "amount_usd": 600.0}
        )
        self.assertEqual([e["kind"] for e in events], ["verified"])

    def test_a_settled_verdict_is_not_rechecked(self) -> None:
        self._verify({"ok": True, "sender": self.wallet, "amount_usd": 600.0})
        self.assertEqual(pool.wallet_proofs_to_check(), [])

        # Nor is a mismatch, so the admin is not re-alerted every minute.
        pool.approve_user(BOB, admin_id=ADMIN)
        self._wallet(BOB)
        req = pool.request_deposit(BOB, 600.0, txid=txhash("7"))
        self.assertTrue(req["ok"], req)
        pool.decide_deposit(req["request_id"], admin_id=ADMIN, approve=True)
        first = self._verify({"ok": True, "sender": "0x" + "9" * 40,
                              "amount_usd": 600.0})
        self.assertEqual([e["kind"] for e in first], ["mismatch"])
        self.assertEqual(self._verify({"ok": True, "sender": "0x" + "9" * 40,
                                       "amount_usd": 600.0}), [])
        self.assertEqual(len(pool.wallet_check_mismatches()), 1)

    def test_an_uncredited_claim_is_no_proof(self) -> None:
        """A pending claim is a hash somebody typed. Only a credited deposit
        is a transfer the venue confirmed, and only that can prove anything."""
        pool.approve_user(BOB, admin_id=ADMIN)
        self._wallet(BOB)
        pool.request_deposit(BOB, 600.0, txid=txhash("8"))
        self.assertEqual(
            [p["telegram_id"] for p in pool.wallet_proofs_to_check()], [ALICE]
        )

    def test_a_deposit_to_the_wrong_address_never_verifies(self) -> None:
        events = self._verify({"ok": False, "reason": "wrong_destination",
                               "actual_to": "0x" + "1" * 40})
        self.assertEqual([e["kind"] for e in events], ["wrong_destination"])
        self.assertEqual(pool.get_wallet(ALICE)["status"], "pending")


class DepositTxidTests(PoolTestCase):
    """The hash is which transfer arrived, and it may be claimed exactly once.

    Deposits from every tester land at one venue address, so without a hash
    a claimed amount is not tied to any particular transfer, and without
    uniqueness two testers could be credited for the same one.
    """

    def setUp(self) -> None:
        super().setUp()
        pool.approve_user(ALICE, admin_id=ADMIN)
        pool.approve_user(BOB, admin_id=ADMIN)
        self._wallet(ALICE)
        self._wallet(BOB)

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

    def test_a_released_card_can_be_accepted_again(self) -> None:
        """The reoffer sweep can bring a card back after the first Accept was
        released. The second Accept used to be refused as a duplicate with
        "you're on this order", which was the opposite of the truth — the
        release had already handed the money back."""
        self._fund(ALICE, 1000.0)
        pool.record_intent("mill_7", ALICE)
        pool.release_intents("mill_7", status="missed")
        self.assertAlmostEqual(
            float(pool.get_account(ALICE)["reserved_usd"]), 0.0, places=2
        )

        again = pool.record_intent("mill_7", ALICE)
        self.assertTrue(again["ok"], again)
        # The money is genuinely held this time, not just claimed. The reserve
        # event dedupes on (telegram_id, kind, ref), so reusing the first
        # attempt's ref would have been dropped in silence and left a pending
        # intent against nothing.
        self.assertAlmostEqual(
            float(pool.get_account(ALICE)["reserved_usd"]),
            float(again["risk_usd"]),
            places=2,
        )
        self.assertEqual(len(pool.pending_intents("mill_7")), 1)

    def test_a_reaccepted_card_gives_the_money_back_again(self) -> None:
        """Release has to work on every attempt, not just the first."""
        self._fund(ALICE, 1000.0)
        for _ in range(3):
            self.assertTrue(pool.record_intent("mill_8", ALICE)["ok"])
            pool.release_intents("mill_8", status="missed")
            self.assertAlmostEqual(
                float(pool.get_account(ALICE)["reserved_usd"]), 0.0, places=2
            )
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)

    def test_being_in_the_trade_is_not_confused_with_having_missed_it(self) -> None:
        self._fund(ALICE, 1000.0)
        pool.record_intent("mill_9", ALICE)
        with pool._write_txn() as conn:
            conn.execute(
                "UPDATE pool_intents SET status = 'pooled' WHERE ref = ?",
                ("mill_9",),
            )
        result = pool.record_intent("mill_9", ALICE)
        self.assertEqual(result["reason"], "already_recorded")
        self.assertEqual(result["status"], "pooled")

    def test_the_attempt_column_reaches_an_existing_ledger(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` is a no-op on a live database, so the
        column only arrives through the explicit migration."""
        with pool._connect() as conn:
            conn.execute("ALTER TABLE pool_intents DROP COLUMN attempt")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(pool_intents)")}
        self.assertNotIn("attempt", cols)

        with pool._connect() as conn:  # reconnect runs _migrate
            cols = {r[1] for r in conn.execute("PRAGMA table_info(pool_intents)")}
        self.assertIn("attempt", cols)

        self._fund(ALICE, 1000.0)
        self.assertTrue(pool.record_intent("mill_10", ALICE)["ok"])

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

    def _age_intent(self, ref: str, minutes: int) -> None:
        when = (datetime.now(timezone.utc)
                - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(self._db)
        with conn:
            conn.execute(
                "UPDATE pool_intents SET created_at = ? WHERE ref = ?",
                (when, ref),
            )
        conn.close()

    def test_an_intent_cannot_hang_forever_on_a_ref_that_looks_active(self) -> None:
        """The shipped bug, at the level it actually broke.

        A tester's Accept exempted the mill idea from expiry, so `mill_<id>`
        stayed in the active-ref set indefinitely and the reserve was never
        returned — the Accept simply went quiet holding their money. The root
        cause is fixed upstream; this is the backstop that makes "an Accept
        always resolves" true even if that bookkeeping breaks again.
        """
        self._fund(ALICE, 1000.0)
        pool.record_intent("mill_1025", ALICE)
        self._age_intent("mill_1025", bot_config.POOL_INTENT_TTL_MIN + 5)

        released = pool.expire_stale_intents({"mill_1025"})

        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["ref"], "mill_1025")
        self.assertEqual(float(pool.get_account(ALICE)["reserved_usd"]), 0.0)
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 1000.0)

    def test_the_backstop_does_not_race_a_fill_that_can_still_happen(self) -> None:
        """A TTL shorter than the real fill window would drop a tester out of
        a trade they were promised, which is worse than releasing late."""
        self.assertGreater(
            bot_config.POOL_INTENT_TTL_MIN,
            bot_config.LIVE_MILL_REOFFER_MAX_AGE_MIN,
        )
        self._fund(ALICE, 1000.0)
        pool.record_intent("mill_1025", ALICE)
        self._age_intent("mill_1025", bot_config.LIVE_MILL_REOFFER_MAX_AGE_MIN + 1)
        self.assertEqual(pool.expire_stale_intents({"mill_1025"}), [])
        self.assertGreater(float(pool.get_account(ALICE)["reserved_usd"]), 0.0)


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


class UnsubscribeTests(PoolTestCase):
    """Removing an account, so an id can onboard again from `/start`.

    This is the only operation that deletes a tester's journal, which makes
    its refusals the interesting part rather than its happy path: every one
    of them is a way the deletion could take money from somebody. The one
    that matters most is an open stake — the account owns a share of a live
    position, and deleting it would hand that share to the other holders on
    the next booked exit.
    """

    def setUp(self) -> None:
        super().setUp()
        self._patch(bot_config, "POOL_MIN_WITHDRAWAL_USD", 50.0)
        self._patch(bot_config, "POOL_WITHDRAWAL_FEE_RESERVE_USD", 3.0)
        self._patch(bot_config, "POOL_MAX_WITHDRAWAL_USD", 2500.0)
        self._patch(bot_config, "POOL_MAX_USER_DAILY_WITHDRAWAL_USD", 2500.0)
        self._patch(bot_config, "POOL_MAX_GLOBAL_DAILY_WITHDRAWAL_USD", 5000.0)
        self._patch(bot_config, "POOL_PAYOUTS_ENABLED", True)
        self._patch(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", False)
        pool.approve_user(ALICE, admin_id=ADMIN)
        self.alice_wallet = self._wallet(ALICE)
        pool.mark_wallet_verified(self.alice_wallet)

    def _patch(self, target, attr, value) -> None:
        p = patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    def _remove(self, uid: int = ALICE, **kw):
        return pool.unsubscribe_user(uid, admin_id=ADMIN, **kw)

    # -- the happy path ----------------------------------------------------

    def test_a_removed_id_is_a_first_contact_user_again(self) -> None:
        """The whole point: onboarding is one-shot per id, so demoing it twice
        needs the account genuinely gone rather than merely quiet."""
        result = self._remove(confirm=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "removed")

        self.assertIsNone(pool.get_account(ALICE))
        self.assertIsNone(pool.get_wallet(ALICE))
        self.assertFalse(pool.is_approved(ALICE))
        # 'new' is what makes `/start` show "request sent for review" and ping
        # an admin to Admit, rather than the welcome.
        self.assertEqual(pool.request_access(ALICE, "alice"), "new")

    def test_the_same_wallet_can_be_registered_again_afterwards(self) -> None:
        """One account per address is enforced by a unique index, so a
        leftover row would refuse the re-registration that the demo needs."""
        self._remove(confirm=True)
        pool.approve_user(ALICE, admin_id=ADMIN)
        again = pool.register_wallet(ALICE, self.alice_wallet)
        self.assertTrue(again["ok"], again)

    def test_a_preview_changes_nothing(self) -> None:
        pool.credit(ALICE, 10.0, admin_id=ADMIN, ref="seed")
        preview = self._remove()
        self.assertEqual(preview["action"], "preview")
        self.assertIn("pool_accounts", preview["counts"])
        # Still there — the card is a question, not the act.
        self.assertIsNotNone(pool.get_account(ALICE))
        self.assertTrue(pool.is_approved(ALICE))

    def test_another_account_is_never_touched(self) -> None:
        pool.approve_user(BOB, admin_id=ADMIN)
        pool.credit(BOB, 500.0, admin_id=ADMIN, ref="seed-bob")
        self._remove(confirm=True)
        self.assertIsNotNone(pool.get_account(BOB))
        self.assertEqual(float(pool.get_account(BOB)["cash_usd"]), 500.0)
        self.assertTrue(pool.is_approved(BOB))

    def test_an_id_with_no_history_is_said_plainly(self) -> None:
        result = self._remove(424242)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "nothing_to_remove")

    # -- money -------------------------------------------------------------

    def test_a_withdrawable_balance_is_refused(self) -> None:
        """Money that can still be paid out must leave as a payout, to the
        address they proved they control — not be written off."""
        pool.credit(ALICE, 500.0, admin_id=ADMIN, ref="seed")
        result = self._remove(confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "balance_withdrawable")
        # Refused means refused: nothing deleted, nothing written off.
        self.assertEqual(float(pool.get_account(ALICE)["cash_usd"]), 500.0)
        self.assertTrue(pool.is_approved(ALICE))

    def test_a_sub_minimum_residue_is_written_off_and_recorded(self) -> None:
        """The unused fee reserve coming back from "take everything out" lands
        below the minimum, so no withdrawal can ever move it. Refusing over it
        would make the account undeletable; writing it off silently would be a
        loss nobody could reconstruct. So it is written off *and* recorded."""
        pool.credit(ALICE, 2.84, admin_id=ADMIN, ref="residue")
        preview = self._remove()
        self.assertAlmostEqual(preview["written_off_usd"], 2.84, places=2)

        result = self._remove(confirm=True)
        self.assertTrue(result["ok"], result)
        self.assertAlmostEqual(result["written_off_usd"], 2.84, places=2)
        self.assertIsNone(pool.get_account(ALICE))
        self.assertEqual(pool.total_tester_cash(), 0.0)

        records = pool.unsubscribe_records(ALICE)
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]["written_off_usd"], 2.84, places=2)
        self.assertEqual(records[0]["by_admin"], ADMIN)
        # The record outlives the rows it describes — that is what makes this
        # an accounted write-off rather than a disappearance.
        self.assertIn("pool_events", records[0]["rows_deleted"])

    def test_an_empty_account_needs_no_write_off(self) -> None:
        result = self._remove(confirm=True)
        self.assertEqual(result["written_off_usd"], 0.0)

    # -- the refusals that protect other people's money --------------------

    def test_an_open_stake_is_refused(self) -> None:
        """The account owns a share of a position that is still live. Deleting
        it would leave that share owned by nobody, and the next exit would
        split the trade between the remaining holders."""
        pool.credit(ALICE, 1000.0, admin_id=ADMIN, ref="seed")
        pool.record_intent("cycle-1", ALICE)
        opened = pool.open_stakes(
            7, "cycle-1", fill_qty=0.01, fill_price=100_000.0,
            risk_per_unit=10.0, house_risk_usd=14.0,
        )
        self.assertTrue(opened, "fixture failed to open a stake")

        result = self._remove(confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "open_stake")
        self.assertIsNotNone(pool.get_account(ALICE))
        self.assertTrue(pool.open_stakes_for(7))

    def test_a_pending_accept_is_refused(self) -> None:
        pool.credit(ALICE, 1000.0, admin_id=ADMIN, ref="seed")
        pool.record_intent("cycle-2", ALICE)
        result = self._remove(confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "pending_intent")
        self.assertIsNotNone(pool.get_account(ALICE))

    def test_a_withdrawal_in_flight_is_refused(self) -> None:
        """That row is the only evidence a send may already have happened, and
        Coinbase offers no way to ask."""
        pool.credit(ALICE, 1000.0, admin_id=ADMIN, ref="seed")
        req = pool.request_withdrawal(ALICE, 990.0)
        self.assertTrue(req["ok"], req)
        # Balance is now below the minimum, so only this guard can stop it.
        self.assertLess(float(pool.get_account(ALICE)["cash_usd"]), 50.0)

        result = self._remove(confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "withdrawal_in_flight")
        self.assertIsNotNone(
            pool.get_withdrawal(int(req["withdrawal_id"])),
            "the payout row must survive a refused removal",
        )

    def test_a_settled_withdrawal_does_not_block_removal(self) -> None:
        """Only an *open* payout is evidence of money in motion."""
        pool.credit(ALICE, 1000.0, admin_id=ADMIN, ref="seed")
        req = pool.request_withdrawal(ALICE, 990.0)
        wid = int(req["withdrawal_id"])
        pool.decide_withdrawal(wid, admin_id=ADMIN, approve=True)
        with pool._connect() as conn:
            conn.execute(
                "UPDATE pool_withdrawals SET status = 'settled' WHERE id = ?",
                (wid,),
            )
        self.assertTrue(self._remove(confirm=True)["ok"])

    def test_a_pending_deposit_claim_is_refused(self) -> None:
        """Money is on its way to this account and the sweep is about to
        credit it."""
        pool.request_deposit(ALICE, 1000.0, txid=txhash("a"))
        result = self._remove(confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "deposit_pending")

    # -- the chain-deposit record ------------------------------------------

    def test_a_chain_deposit_is_detached_rather_than_deleted(self) -> None:
        """Deleting it would make the sweep re-see a historical transfer as
        money that arrived with nobody to own it, and page an admin about it.
        Keeping the row as `baseline` preserves "already seen" while dropping
        the link to the person."""
        with pool._connect() as conn:
            conn.execute(
                "INSERT INTO pool_chain_deposits (cb_tx_id, txid, amount_usd, "
                "status, telegram_id, first_seen_at) VALUES "
                "(?, ?, ?, 'credited', ?, ?)",
                ("cb-1", txhash("b"), 500.0, ALICE, "2026-09-16T00:00:00Z"),
            )

        self.assertTrue(self._remove(confirm=True)["ok"])

        with pool._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pool_chain_deposits WHERE cb_tx_id = 'cb-1'"
            ).fetchone()
        self.assertIsNotNone(row, "the transfer record must survive")
        self.assertIsNone(row["telegram_id"])
        self.assertEqual(row["status"], "baseline")
        # 'baseline' is what makes the sweep skip it instead of alerting.
        self.assertEqual(pool.unmatched_chain_deposits(), [])


if __name__ == "__main__":
    unittest.main()
