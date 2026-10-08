"""Hot-wallet signer: the one path that moves tester money on-chain.

Pinned properties:
- The signer refuses to exist unless the key derives to TEST_WALLET_ADDRESS.
- Destinations are exactly the two configured venue deposit addresses.
- A leg is claimed in the journal *before* broadcast, so a double-tap, a
  second admin, or a crash cannot send it twice; a pre-broadcast failure
  hands the leg back untouched.
- Per-leg and rolling-24h caps refuse before any RPC is made.
- A signed tx recovers to the test wallet, targets the pinned USDC
  contract on the right chain, and encodes transfer(to, amount).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_config
import chain
import config
import pool
import signer
import treasury

ADMIN = 111

# A throwaway dev key — never funded, only here so the signer has something
# to derive an address from. The address is derived, not pasted.
DEV_KEY = "0x" + "11" * 32
from eth_account import Account as _Account  # noqa: E402

DEV_ADDRESS = _Account.from_key(DEV_KEY).address
COINBASE_ADDRESS = "0xDdA10FB6e6d726ae1cfB079CD79A4f0Ef7cAF240"
KALSHI_ADDRESS = "0x1111111111111111111111111111111111111111"
TXID = "0x" + "ab" * 32


class FakeNode:
    """Just enough JSON-RPC to let send_usdc build, sign and broadcast."""

    def __init__(self, *, usdc: float = 1000.0, eth_wei: int = 10**17):
        self.usdc = usdc
        self.eth_wei = eth_wei
        self.calls: list[tuple[str, list, int]] = []
        self.broadcast: list[str] = []
        self.refuse_broadcast: str | None = None

    def rpc(self, method: str, params: list, *, chain_id: int):
        self.calls.append((method, params, chain_id))
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": hex(10**9)}
        if method == "eth_maxPriorityFeePerGas":
            return hex(10**8)
        if method == "eth_getBalance":
            return hex(self.eth_wei)
        if method == "eth_estimateGas":
            return hex(48_000)
        if method == "eth_getTransactionCount":
            return hex(7)
        if method == "eth_sendRawTransaction":
            if self.refuse_broadcast:
                raise chain.ChainError(self.refuse_broadcast)
            self.broadcast.append(params[0])
            return TXID
        raise AssertionError(f"unexpected rpc {method}")

    def balance(self, address: str, *, chain_id: int) -> float:
        return self.usdc


class SignerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "TEST_WALLET_ADDRESS", DEV_ADDRESS),
            patch.object(config, "TEST_WALLET_PRIVATE_KEY", DEV_KEY),
            patch.object(config, "POOL_DEPOSIT_ADDRESS", COINBASE_ADDRESS),
            patch.object(config, "POOL_DEPOSIT_CHAIN_ID", 8453, create=True),
            patch.object(config, "KALSHI_DEPOSIT_ADDRESS", KALSHI_ADDRESS, create=True),
            patch.object(config, "KALSHI_DEPOSIT_CHAIN_ID", 1, create=True),
            patch.object(config, "TREASURY_SEND_MAX_USD", 500.0, create=True),
            patch.object(config, "TREASURY_SEND_DAILY_MAX_USD", 800.0, create=True),
            patch.object(config, "BASE_RPC_URL", "https://base.invalid"),
            patch.object(config, "ETH_RPC_URL", "https://eth.invalid"),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
            patch.object(bot_config, "POOL_DEPLOY_FEE_USD", 0.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        pool.init_db()
        self.node = FakeNode()
        for target, attr in ((chain, "_rpc"), (chain, "_usdc_balance_rpc")):
            p = patch.object(
                target, attr,
                self.node.rpc if attr == "_rpc" else self.node.balance,
            )
            p.start()
            self.addCleanup(p.stop)

    def _leg(self, to_loc: str, amount: float, from_loc: str = "test_wallet") -> int:
        result = treasury.request_transfer(from_loc, to_loc, amount, admin_id=ADMIN)
        self.assertTrue(result["ok"], result)
        return int(result["transfer_id"])


class SignerStatusTests(SignerTestCase):
    def test_enabled_only_when_key_matches_wallet(self) -> None:
        self.assertTrue(signer.status()["enabled"])
        with patch.object(config, "TEST_WALLET_ADDRESS", COINBASE_ADDRESS):
            st = signer.status()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["reason"], "key_address_mismatch")
        with patch.object(config, "TEST_WALLET_PRIVATE_KEY", None):
            self.assertEqual(signer.status()["reason"], "no_key")
        with patch.object(config, "TEST_WALLET_PRIVATE_KEY", "0xnotakey"):
            self.assertEqual(signer.status()["reason"], "bad_key")

    def test_destinations_are_exactly_the_two_venue_addresses(self) -> None:
        cb = signer.destination("coinbase")
        self.assertEqual((cb["address"], cb["chain_id"]), (COINBASE_ADDRESS, 8453))
        ks = signer.destination("kalshi")
        self.assertEqual((ks["address"], ks["chain_id"]), (KALSHI_ADDRESS, 1))
        self.assertIsNone(signer.destination("test_wallet"))
        self.assertIsNone(signer.destination("anything_else"))
        with patch.object(config, "KALSHI_DEPOSIT_CHAIN_ID", None):
            self.assertIsNone(signer.destination("kalshi"))
        with patch.object(config, "POOL_DEPOSIT_CHAIN_ID", 137):  # no pinned USDC
            self.assertIsNone(signer.destination("coinbase"))
        with patch.object(config, "POOL_DEPOSIT_ADDRESS", "not-an-address"):
            self.assertIsNone(signer.destination("coinbase"))


class SendUsdcTests(SignerTestCase):
    def test_signed_tx_is_a_usdc_transfer_from_the_test_wallet(self) -> None:
        from eth_account import Account
        from eth_account.typed_transactions import TypedTransaction

        sent = signer.send_usdc("coinbase", 123.45)
        self.assertEqual(sent["txid"], TXID)
        self.assertEqual(sent["chain_id"], 8453)
        self.assertEqual(sent["to_address"], COINBASE_ADDRESS)
        self.assertIn("basescan.org", sent["explorer"])
        self.assertEqual(len(self.node.broadcast), 1)

        from hexbytes import HexBytes

        raw = HexBytes(self.node.broadcast[0])
        self.assertEqual(Account.recover_transaction(raw).lower(), DEV_ADDRESS.lower())
        tx = TypedTransaction.from_bytes(raw).as_dict()

        def hexstr(v):
            return v.lower() if isinstance(v, str) else "0x" + bytes(v).hex()

        self.assertEqual(tx["chainId"], 8453)
        self.assertEqual(hexstr(tx["to"]), chain.USDC_CONTRACTS[8453])
        self.assertEqual(tx["value"], 0)
        self.assertEqual(tx["nonce"], 7)
        data = hexstr(tx["data"])
        self.assertTrue(data.startswith("0xa9059cbb"))
        self.assertEqual(data[10:74], chain._pad_address(COINBASE_ADDRESS)[2:])
        self.assertEqual(int(data[74:138], 16), 123_450_000)
        # Every RPC went to the destination's chain, none elsewhere.
        self.assertTrue(all(c == 8453 for _, _, c in self.node.calls))

    def test_kalshi_leg_goes_on_kalshi_chain(self) -> None:
        sent = signer.send_usdc("kalshi", 50.0)
        self.assertEqual(sent["chain_id"], 1)
        self.assertIn("etherscan.io", sent["explorer"])
        self.assertTrue(all(c == 1 for _, _, c in self.node.calls))

    def test_refuses_before_broadcast_when_wallet_is_short(self) -> None:
        self.node.usdc = 10.0
        with self.assertRaises(signer.SignerError) as ctx:
            signer.send_usdc("coinbase", 100.0)
        self.assertIn("USDC", str(ctx.exception))
        self.assertEqual(self.node.broadcast, [])

    def test_refuses_before_broadcast_without_gas(self) -> None:
        self.node.eth_wei = 0
        with self.assertRaises(signer.SignerError) as ctx:
            signer.send_usdc("coinbase", 100.0)
        self.assertIn("gas", str(ctx.exception))
        self.assertEqual(self.node.broadcast, [])

    def test_refuses_when_signer_or_destination_unconfigured(self) -> None:
        with patch.object(config, "TEST_WALLET_PRIVATE_KEY", None):
            with self.assertRaises(signer.SignerError):
                signer.send_usdc("coinbase", 1.0)
        with patch.object(config, "KALSHI_DEPOSIT_ADDRESS", None):
            with self.assertRaises(signer.SignerError):
                signer.send_usdc("kalshi", 1.0)
        self.assertEqual(self.node.calls, [])

    def test_ensure_gas_skips_when_already_funded(self) -> None:
        result = signer.ensure_gas(1, reserve_usdc=100.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("skipped"), "funded")
        self.assertEqual(self.node.broadcast, [])

    def test_ensure_gas_needs_bootstrap_when_eth_is_zero(self) -> None:
        self.node.eth_wei = 0
        result = signer.ensure_gas(1, reserve_usdc=0.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "bootstrap_needed")
        self.assertEqual(self.node.broadcast, [])


class ExecuteTransferTests(SignerTestCase):
    def test_happy_path_claims_then_records_txid(self) -> None:
        tid = self._leg("coinbase", 200.0)
        result = treasury.execute_transfer(tid, admin_id=ADMIN)
        self.assertTrue(result["ok"], result)
        row = treasury.get_transfer(tid)
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["txid"], TXID)
        self.assertIsNotNone(row["sent_at"])
        self.assertIn("[signer] sent", row["note"])
        self.assertEqual(len(self.node.broadcast), 1)

    def test_execute_transfer_refuses_cleanly_when_gas_needs_bootstrap(self) -> None:
        tid = self._leg("coinbase", 200.0)
        self.node.eth_wei = 0
        result = treasury.execute_transfer(tid, admin_id=ADMIN)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "signer_refused")
        self.assertIn("gas", str(result.get("detail") or "").lower())
        self.assertEqual(treasury.get_transfer(tid)["status"], "pending_send")
        self.assertEqual(self.node.broadcast, [])

    def test_retry_pending_deploy_sends_moves_a_stuck_leg(self) -> None:
        move = treasury.journal_deploy_move(
            telegram_id=42, strategy="kalshi_wick", delta_usd=50.0, admin_id=ADMIN,
        )
        self.assertTrue(move["ok"], move)
        tid = int(move["transfer_id"])
        # Pretend the first auto-send failed for gas, then ETH arrived.
        self.node.eth_wei = 0
        blocked = treasury.execute_transfer(tid, admin_id=ADMIN)
        self.assertFalse(blocked["ok"])
        self.node.eth_wei = 10**17
        results = treasury.retry_pending_deploy_sends(admin_id=ADMIN)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"], results[0])
        self.assertEqual(treasury.get_transfer(tid)["status"], "sent")
        self.assertEqual(len(self.node.broadcast), 1)

    def test_second_send_cannot_double_broadcast(self) -> None:
        tid = self._leg("coinbase", 200.0)
        self.assertTrue(treasury.execute_transfer(tid, admin_id=ADMIN)["ok"])
        again = treasury.execute_transfer(tid, admin_id=ADMIN)
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "not_pending")
        self.assertEqual(len(self.node.broadcast), 1)

    def test_claim_is_atomic_against_a_concurrent_claim(self) -> None:
        tid = self._leg("coinbase", 200.0)
        # Race: our policy check passed, then another admin's tap claimed the
        # row before our UPDATE ran. The claim must lose, not broadcast.
        real_check = treasury.signer_check

        def check_then_lose(transfer_id):
            result = real_check(transfer_id)
            self.assertTrue(treasury._claim_for_send(transfer_id, admin_id=999))
            return result

        with patch.object(treasury, "signer_check", side_effect=check_then_lose):
            result = treasury.execute_transfer(tid, admin_id=ADMIN)
        self.assertEqual(result["reason"], "already_claimed")
        self.assertEqual(self.node.broadcast, [])
        self.assertEqual(treasury.get_transfer(tid)["status"], "sent")

    def test_broadcast_failure_releases_the_leg(self) -> None:
        tid = self._leg("coinbase", 200.0)
        self.node.refuse_broadcast = "nonce too low"
        result = treasury.execute_transfer(tid, admin_id=ADMIN)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "signer_refused")
        row = treasury.get_transfer(tid)
        self.assertEqual(row["status"], "pending_send")
        self.assertIsNone(row["txid"])
        self.assertIsNone(row["sent_at"])
        self.assertIn("signer failed", row["note"])
        self.assertNotIn("broadcasting", row["note"])
        # And it can be retried once the cause is fixed.
        self.node.refuse_broadcast = None
        self.assertTrue(treasury.execute_transfer(tid, admin_id=ADMIN)["ok"])

    def test_preflight_failure_releases_the_leg(self) -> None:
        tid = self._leg("coinbase", 200.0)
        self.node.usdc = 1.0
        result = treasury.execute_transfer(tid, admin_id=ADMIN)
        self.assertEqual(result["reason"], "signer_refused")
        self.assertEqual(treasury.get_transfer(tid)["status"], "pending_send")
        self.assertEqual(self.node.broadcast, [])

    def test_per_leg_cap_refuses_without_touching_the_chain(self) -> None:
        tid = self._leg("coinbase", 500.01)
        result = treasury.execute_transfer(tid, admin_id=ADMIN)
        self.assertEqual(result["reason"], "over_leg_cap")
        self.assertEqual(treasury.get_transfer(tid)["status"], "pending_send")
        self.assertEqual(self.node.calls, [])

    def test_daily_cap_counts_only_signer_sent_legs(self) -> None:
        # A hand-sent leg does not count against the signer's budget.
        manual = self._leg("coinbase", 500.0)
        treasury.mark_sent(manual, txid=TXID, admin_id=ADMIN)
        a = self._leg("coinbase", 450.0)
        self.assertTrue(treasury.execute_transfer(a, admin_id=ADMIN)["ok"])
        self.assertAlmostEqual(treasury.signer_sent_usd_last_24h(), 450.0)
        b = self._leg("kalshi", 400.0)  # 450 + 400 > 800
        result = treasury.execute_transfer(b, admin_id=ADMIN)
        self.assertEqual(result["reason"], "over_daily_cap")
        self.assertEqual(result["used_usd"], 450.0)
        self.assertEqual(treasury.get_transfer(b)["status"], "pending_send")
        c = self._leg("kalshi", 350.0)  # exactly at the cap is fine
        self.assertTrue(treasury.execute_transfer(c, admin_id=ADMIN)["ok"])

    def test_only_test_wallet_origin_legs_are_signable(self) -> None:
        tid = self._leg("kalshi", 100.0, from_loc="coinbase")
        result = treasury.execute_transfer(tid, admin_id=ADMIN)
        self.assertEqual(result["reason"], "not_from_test_wallet")
        back = self._leg("test_wallet", 100.0, from_loc="kalshi")
        self.assertEqual(
            treasury.execute_transfer(back, admin_id=ADMIN)["reason"],
            "not_from_test_wallet",
        )
        self.assertEqual(self.node.calls, [])

    def test_signer_check_mirrors_execute_refusals(self) -> None:
        tid = self._leg("kalshi", 100.0)
        self.assertTrue(treasury.signer_check(tid)["ok"])
        with patch.object(config, "KALSHI_DEPOSIT_ADDRESS", None):
            self.assertEqual(
                treasury.signer_check(tid)["reason"], "no_allowlisted_destination"
            )
        with patch.object(config, "TEST_WALLET_PRIVATE_KEY", None):
            self.assertEqual(treasury.signer_check(tid)["reason"], "signer_no_key")
        self.assertEqual(treasury.signer_check(9999)["reason"], "not_found")


class ConfirmSweepKalshiTests(SignerTestCase):
    def test_kalshi_leg_confirms_on_chain_when_deposit_address_known(self) -> None:
        tid = self._leg("kalshi", 100.0)
        self.assertTrue(treasury.execute_transfer(tid, admin_id=ADMIN)["ok"])

        def fake_find(txid, *, to_address=None, chain_id=1):
            if to_address and to_address.lower() == KALSHI_ADDRESS.lower():
                return {"txid": txid, "to": to_address.lower(),
                        "amount_usd": 100.0, "confirmations": 30}
            return None

        with patch.object(chain, "readable", return_value=True), \
                patch.object(chain, "find_transfer", side_effect=fake_find):
            confirmed = treasury.confirm_sweep()
        self.assertEqual([c["id"] for c in confirmed], [tid])
        self.assertEqual(treasury.get_transfer(tid)["status"], "confirmed")

    def test_kalshi_leg_stays_sent_without_deposit_address(self) -> None:
        tid = self._leg("kalshi", 100.0)
        treasury.mark_sent(tid, txid=TXID, admin_id=ADMIN)
        with patch.object(config, "KALSHI_DEPOSIT_ADDRESS", None), \
                patch.object(chain, "readable", return_value=True), \
                patch.object(chain, "find_transfer") as find:
            confirmed = treasury.confirm_sweep()
        self.assertEqual(confirmed, [])
        find.assert_not_called()
        self.assertEqual(treasury.get_transfer(tid)["status"], "sent")


class PayoutRailTests(SignerTestCase):
    """Withdrawals paid from the test wallet by the signer."""

    USER = 7001

    def setUp(self) -> None:
        super().setUp()
        for p in (
            patch.object(bot_config, "POOL_MIN_EQUITY_USD", 10.0),
            patch.object(bot_config, "POOL_MIN_DEPOSIT_USD", 20.0),
            patch.object(bot_config, "POOL_MIN_WITHDRAWAL_USD", 10.0),
            patch.object(bot_config, "POOL_PAYOUTS_ENABLED", True),
            patch.object(bot_config, "POOL_AUTO_APPROVE_WITHDRAWALS", True),
            patch.object(bot_config, "POOL_WITHDRAWAL_FEE_RESERVE_USD", 3.0),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.user_address = "0x" + f"{self.USER:040x}"
        pool.approve_user(self.USER, admin_id=ADMIN)
        pool.register_wallet(self.USER, self.user_address)
        for cid in (8453, 1):
            pool.observe_testwallet_deposits([], chain_id=cid)

    def _deposit_on(self, chain_id: int, amount: float, seed: str = "a") -> None:
        pool.observe_testwallet_deposits([{
            "txid": "0x" + (seed * 64)[:64], "from": self.user_address,
            "amount_usd": amount, "confirmations": 30,
        }], chain_id=chain_id)

    def test_payout_chains_are_the_chains_the_user_proved(self) -> None:
        self.assertEqual(pool.payout_chains_for(self.USER), [])
        self._deposit_on(8453, 200.0, "a")
        self.assertEqual(pool.payout_chains_for(self.USER), [8453])
        self._deposit_on(1, 100.0, "b")
        self.assertEqual(set(pool.payout_chains_for(self.USER)), {1, 8453})

    def test_send_usdc_payout_reads_the_address_from_the_ledger(self) -> None:
        self._deposit_on(1, 200.0)
        sent = signer.send_usdc_payout(self.USER, 50.0, chain_id=1)
        self.assertEqual(sent["to_address"].lower(), self.user_address.lower())
        self.assertEqual(sent["chain_id"], 1)
        # Calldata targets the user's address, nothing the caller chose.
        data = self.node.calls[[m for m, _, _ in self.node.calls].index("eth_estimateGas")][1][0]["data"]
        self.assertEqual(data[10:74], chain._pad_address(self.user_address)[2:])

    def test_send_usdc_payout_refuses_unverified_users(self) -> None:
        stranger = 7002
        pool.approve_user(stranger, admin_id=ADMIN)
        pool.register_wallet(stranger, "0x" + f"{stranger:040x}")  # never deposited
        with self.assertRaises(signer.SignerError) as ctx:
            signer.send_usdc_payout(stranger, 10.0, chain_id=1)
        self.assertIn("unverified", str(ctx.exception))
        self.assertEqual(self.node.broadcast, [])

    def test_watchdog_pays_from_test_wallet_when_it_holds_the_balance(self) -> None:
        import watchdog

        self._deposit_on(1, 200.0)
        req = pool.request_withdrawal(self.USER, 100.0)
        self.assertTrue(req["ok"], req)
        wid = int(req["withdrawal_id"])
        self.node.usdc = 500.0
        with patch("notify.send_pool_dm") as dm, \
                patch("notify.send_pool_admin_alert") as alert, \
                patch("payouts.usdc_account") as coinbase:
            watchdog._payout_sweep()
        coinbase.assert_not_called()
        row = next(r for r in pool.pending_withdrawals("submitted") if r["id"] == wid)
        self.assertEqual(row["source"], "test_wallet")
        self.assertEqual(row["chain_id"], 1)
        self.assertEqual(row["txid"], TXID)
        self.assertEqual(float(row["fee_usd"]), 0.0)
        # No fee on this rail: the $3 reserve went back to the user.
        self.assertAlmostEqual(float(row["debited_usd"]), 100.0, places=2)
        self.assertAlmostEqual(float(pool.get_account(self.USER)["cash_usd"]), 100.0, places=2)
        self.assertTrue(dm.called)
        self.assertIn("test wallet", alert.call_args[0][0])
        self.assertEqual(len(self.node.broadcast), 1)

    def test_watchdog_falls_back_to_coinbase_when_test_wallet_is_short(self) -> None:
        import watchdog

        self._deposit_on(1, 200.0)
        req = pool.request_withdrawal(self.USER, 100.0)
        wid = int(req["withdrawal_id"])
        self.node.usdc = 20.0  # wallet cannot cover it
        with patch("notify.send_pool_dm"), patch("notify.send_pool_admin_alert"), \
                patch("payouts.usdc_account", return_value={"id": "acct", "balance": 1000.0}), \
                patch("payouts.send", return_value={"id": "cb-1", "fee_usd": 1.5, "txid": None}) as cb_send:
            watchdog._payout_sweep()
        cb_send.assert_called_once()
        row = next(r for r in pool.pending_withdrawals("submitted") if r["id"] == wid)
        self.assertEqual(row["source"], "coinbase")
        self.assertEqual(self.node.broadcast, [])

    def test_watchdog_falls_back_when_signer_refuses_preflight(self) -> None:
        import watchdog

        self._deposit_on(1, 200.0)
        req = pool.request_withdrawal(self.USER, 100.0)
        wid = int(req["withdrawal_id"])
        self.node.usdc = 500.0
        self.node.eth_wei = 0  # no gas: refused before broadcast
        with patch("notify.send_pool_dm"), patch("notify.send_pool_admin_alert"), \
                patch("payouts.usdc_account", return_value={"id": "acct", "balance": 1000.0}), \
                patch("payouts.send", return_value={"id": "cb-2", "fee_usd": 1.0, "txid": None}) as cb_send:
            watchdog._payout_sweep()
        cb_send.assert_called_once()
        self.assertEqual(
            next(r for r in pool.pending_withdrawals("submitted") if r["id"] == wid)["source"],
            "coinbase",
        )

    def test_ambiguous_broadcast_halts_instead_of_refunding(self) -> None:
        import watchdog

        self._deposit_on(1, 200.0)
        req = pool.request_withdrawal(self.USER, 100.0)
        wid = int(req["withdrawal_id"])
        self.node.usdc = 500.0
        self.node.refuse_broadcast = "rpc eth_sendRawTransaction failed after 3 attempts: timeout"
        with patch("notify.send_pool_dm"), patch("notify.send_pool_admin_alert") as alert, \
                patch("payouts.usdc_account") as coinbase:
            watchdog._payout_sweep()
        coinbase.assert_not_called()
        self.assertEqual(pool.pending_withdrawals("unknown")[0]["id"], wid)
        self.assertIsNotNone(pool.payouts_halted())
        self.assertIn("UNKNOWN", alert.call_args[0][0])
        # Money stays debited: nothing was refunded.
        self.assertAlmostEqual(float(pool.get_account(self.USER)["cash_usd"]), 97.0, places=2)

    def test_settle_sweep_confirms_signer_payout_by_receipt(self) -> None:
        import watchdog

        self._deposit_on(1, 200.0)
        req = pool.request_withdrawal(self.USER, 100.0)
        wid = int(req["withdrawal_id"])
        self.node.usdc = 500.0
        with patch("notify.send_pool_dm"), patch("notify.send_pool_admin_alert"), \
                patch("payouts.usdc_account"):
            watchdog._payout_sweep()

        def fake_find(txid, *, to_address=None, chain_id=1):
            return {"txid": txid, "to": (to_address or "").lower(),
                    "amount_usd": 100.0, "confirmations": 20}

        with patch.object(chain, "readable", return_value=True), \
                patch.object(chain, "find_transfer", side_effect=fake_find), \
                patch.object(chain, "configured", return_value=False), \
                patch("notify.send_pool_dm") as dm:
            watchdog._settle_sweep()
        self.assertEqual(pool.pending_withdrawals("settled")[0]["id"], wid)
        self.assertIn("confirmed", dm.call_args[0][1].lower())


class CoverageGateTests(SignerTestCase):
    """Deploy soft-lock must not outrun physical USDC in the intake wallet."""

    def test_journal_deploy_refuses_when_wallet_is_short(self) -> None:
        # Mirror the live shortfall: $496 on-chain, $21 undeployed elsewhere,
        # so a fresh $500 Kalshi deploy cannot leave the intake wallet.
        self.node.usdc = 496.0
        with patch.object(pool, "undeployed_claims_usd", return_value=21.0):
            blocked = treasury.journal_deploy_move(
                telegram_id=42, strategy="kalshi_wick",
                delta_usd=500.0, admin_id=ADMIN,
            )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "intake_short")
        self.assertEqual(blocked["need_usd"], 500.0)
        self.assertAlmostEqual(blocked["deployable_usd"], 475.0, places=2)
        self.assertEqual(treasury.open_transfers(), [])

    def test_journal_deploy_succeeds_when_coverage_clears(self) -> None:
        self.node.usdc = 521.0
        with patch.object(pool, "undeployed_claims_usd", return_value=21.0):
            move = treasury.journal_deploy_move(
                telegram_id=42, strategy="kalshi_wick",
                delta_usd=500.0, admin_id=ADMIN,
            )
        self.assertTrue(move["ok"], move)
        self.assertEqual(move["amount_usd"], 500.0)

    def test_retry_skips_short_leg_without_spamming_signer(self) -> None:
        # Force a pending deploy leg into the journal, then empty the wallet.
        with patch.object(pool, "undeployed_claims_usd", return_value=0.0):
            move = treasury.journal_deploy_move(
                telegram_id=42, strategy="kalshi_wick",
                delta_usd=500.0, admin_id=ADMIN,
            )
        self.assertTrue(move["ok"], move)
        tid = int(move["transfer_id"])
        self.node.usdc = 496.0
        with patch.object(pool, "undeployed_claims_usd", return_value=21.0):
            results = treasury.retry_pending_deploy_sends(admin_id=ADMIN)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["reason"], "intake_short")
        self.assertTrue(results[0].get("skipped"))
        self.assertEqual(treasury.get_transfer(tid)["status"], "pending_send")
        self.assertEqual(self.node.broadcast, [])

    def test_ensure_gas_will_not_spend_into_leg_reserve(self) -> None:
        # Wallet has $500 USDC and low ETH — top-up would want ~$25, but the
        # whole balance is reserved for the venue send.
        self.node.usdc = 500.0
        self.node.eth_wei = int(0.002 * 1e18)  # above bootstrap, below target
        result = signer.ensure_gas(1, reserve_usdc=500.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "insufficient_usdc_for_gas")
        self.assertEqual(self.node.broadcast, [])


if __name__ == "__main__":
    unittest.main()
