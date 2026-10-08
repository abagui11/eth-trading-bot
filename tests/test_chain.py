"""Chain reads — the evidence layer under deposits and payouts.

Coinbase reports that a deposit arrived but never who sent it, and will not
return a payout's status at all. Everything here exists to answer those two
questions from the chain instead. The properties worth pinning are about
refusing to be fooled and about not converting ignorance into a verdict.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import chain
import config

OURS = "0xdda10fb6e6d726ae1cfb079cd79a4f0ef7caf240"
THEIRS = "0x6549b1e2c9b3b004fca5e3c13ad8189cf2f273b1"
STRANGER = "0x" + "9" * 40
TXID = "0x" + "a" * 64


def transfer(*, sender=THEIRS, recipient=OURS, **over):
    """A tokentx row shaped the way Etherscan returns them."""
    row = {
        "hash": TXID, "from": sender, "to": recipient,
        "value": "600000000",          # 6-decimal USDC base units
        "tokenDecimal": "6", "tokenSymbol": "USDC",
        "confirmations": "50", "timeStamp": "1700000000",
        "blockNumber": "18000000",
    }
    row.update(over)
    return row


class ParseTests(unittest.TestCase):
    def setUp(self) -> None:
        p = patch.object(config, "ETHERSCAN_API_KEY", "k")
        p.start()
        self.addCleanup(p.stop)

    def test_base_units_become_dollars(self) -> None:
        """A decimals slip is a factor of a million in a money path."""
        with patch.object(chain, "_request", return_value=[transfer()]):
            self.assertEqual(
                chain.usdc_transfers(OURS)[0]["amount_usd"], 600.0
            )

    def test_direction_comes_from_the_to_field(self) -> None:
        """tokentx returns both legs for an address. Counting an outgoing
        transfer as inbound would credit somebody for money we sent."""
        rows = [
            transfer(),
            transfer(hash="0x" + "b" * 64, sender=OURS, recipient=THEIRS),
        ]
        with patch.object(chain, "_request", return_value=rows):
            inbound = chain.inbound_usdc(OURS)
        self.assertEqual([t["to"] for t in inbound], [OURS])

    def test_addresses_are_lowercased(self) -> None:
        """Checksummed and lowercase spellings of one address must compare
        equal, or a verified wallet reads as a stranger's."""
        checksummed = "0x6549B1E2C9B3b004fca5E3C13AD8189Cf2f273B1"
        with patch.object(chain, "_request",
                          return_value=[transfer(sender=checksummed)]):
            self.assertEqual(chain.usdc_transfers(OURS)[0]["from"], THEIRS)

    def test_no_transactions_found_is_empty_not_an_error(self) -> None:
        """Etherscan signals an empty result with status "0". Reading that as
        a failure would make a quiet address look like an outage."""
        payload = {"status": "0", "message": "No transactions found",
                   "result": []}
        with patch("chain.requests.get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = payload
            self.assertEqual(chain.usdc_transfers(OURS), [])

    def test_a_missing_key_raises_rather_than_returning_empty(self) -> None:
        """Silently returning "no transfers" would make an unconfigured
        deployment look like one where nobody had deposited."""
        with patch.object(config, "ETHERSCAN_API_KEY", None):
            self.assertFalse(chain.configured())
            with self.assertRaises(chain.ChainError):
                chain.usdc_transfers(OURS)


class VerifyDepositTests(unittest.TestCase):
    def setUp(self) -> None:
        p = patch.object(config, "ETHERSCAN_API_KEY", "k")
        p.start()
        self.addCleanup(p.stop)

    def _verify(self, rows, **kw):
        with patch.object(chain, "_request", return_value=rows):
            return chain.verify_deposit(TXID, to_address=OURS, **kw)

    def test_a_matching_transfer_reports_its_sender(self) -> None:
        result = self._verify([transfer()])
        self.assertTrue(result["ok"])
        self.assertEqual(result["sender"], THEIRS)
        self.assertEqual(result["amount_usd"], 600.0)

    def test_a_transfer_elsewhere_is_not_our_deposit(self) -> None:
        """Somebody could cite a real transaction that has nothing to do with
        us. Only one that landed at our address is proof of a deposit."""
        result = self._verify([transfer(recipient=STRANGER)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "wrong_destination")

    def test_a_shallow_transfer_waits(self) -> None:
        result = self._verify([transfer(confirmations="2")])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unconfirmed")
        # The sender is still reported, so the caller can log progress.
        self.assertEqual(result["sender"], THEIRS)

    def test_an_unknown_hash_is_not_found(self) -> None:
        result = self._verify([transfer(hash="0x" + "c" * 64)])
        self.assertEqual(result["reason"], "not_found")

    def test_a_malformed_hash_is_refused_without_a_lookup(self) -> None:
        with patch.object(chain, "_request") as req:
            self.assertEqual(
                chain.verify_deposit("nope", to_address=OURS)["reason"],
                "not_found",
            )
            req.assert_not_called()

    def test_an_outage_is_reported_as_an_error_not_a_denial(self) -> None:
        """The distinction the whole design rests on: "we could not check" is
        not "it was not them". Collapsing them refuses honest testers."""
        with patch.object(chain, "_request",
                          side_effect=chain.ChainError("503")):
            result = chain.verify_deposit(TXID, to_address=OURS)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "lookup_failed")


class ConfirmPayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        p = patch.object(config, "ETHERSCAN_API_KEY", "k")
        p.start()
        self.addCleanup(p.stop)

    def _confirm(self, rows, amount=600.0, after=1699999999):
        with patch.object(chain, "_request", return_value=rows):
            return chain.confirm_payout(THEIRS, amount,
                                        after_timestamp=after)

    def test_an_arrival_of_the_right_size_confirms(self) -> None:
        result = self._confirm([transfer(sender=OURS, recipient=THEIRS)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["txid"], TXID)

    def test_an_older_transfer_of_the_same_size_does_not_confirm(self) -> None:
        """Without the time floor, a tester's earlier withdrawal of the same
        amount would mark this one settled and hide a payout that never
        arrived."""
        stale = transfer(sender=OURS, recipient=THEIRS,
                         timeStamp="1600000000")
        self.assertFalse(self._confirm([stale])["ok"])

    def test_nothing_yet_is_not_a_failure(self) -> None:
        result = self._confirm([])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_seen_yet")

    def test_an_outage_does_not_read_as_settled(self) -> None:
        with patch.object(chain, "_request",
                          side_effect=chain.ChainError("timeout")):
            result = chain.confirm_payout(THEIRS, 600.0, after_timestamp=0)
        self.assertEqual(result["reason"], "lookup_failed")


# ---------------------------------------------------------------------------
# JSON-RPC source — what keeps Base readable without a paid indexer
# ---------------------------------------------------------------------------

BASE = 8453
WALLET = "0xab1b6cc522c3ec7bdea22598f6e510e7e752479d"
LATEST = 52_000_000


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def transfer_log(*, sender=THEIRS, recipient=WALLET, amount_units=600_000_000,
                 block=LATEST - 30, txid=TXID) -> dict:
    """An eth_getLogs entry for a USDC Transfer, the way a node returns it."""
    return {
        "address": chain.USDC_CONTRACTS[BASE],
        "topics": [chain._TRANSFER_TOPIC, _topic(sender), _topic(recipient)],
        "data": hex(amount_units),
        "blockNumber": hex(block),
        "transactionHash": txid,
    }


class RpcScanTests(unittest.TestCase):
    def setUp(self) -> None:
        chain._etherscan_unsupported.clear()
        for p in (
            patch.object(config, "BASE_RPC_URL", "https://rpc.test"),
            patch.object(config, "RPC_LOG_RANGE", 500),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _rpc(self, logs_by_call=None, *, latest=LATEST):
        """A fake node: eth_blockNumber, eth_getLogs (per chunk), eth_call."""
        calls: list[tuple[str, list]] = []
        logs_by_call = list(logs_by_call or [])

        def fake(method, params, *, chain_id):
            calls.append((method, params))
            if method == "eth_blockNumber":
                return hex(latest)
            if method == "eth_getLogs":
                return logs_by_call.pop(0) if logs_by_call else []
            if method == "eth_call":
                return hex(1_234_560_000)
            raise AssertionError(method)

        return fake, calls

    def test_log_becomes_a_transfer_row_with_confirmations(self) -> None:
        """Same row shape as the Etherscan path, so the watcher does not care
        which source answered. Confirmations are tip - block + 1."""
        fake, _ = self._rpc([[transfer_log(block=LATEST - 11)]])
        with patch.object(chain, "_rpc", side_effect=fake):
            rows, tip = chain.scan_inbound_usdc(WALLET, chain_id=BASE,
                                                from_block=LATEST - 100)
        self.assertEqual(tip, LATEST)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["from"], THEIRS)
        self.assertEqual(row["to"], WALLET)
        self.assertEqual(row["amount_usd"], 600.0)
        self.assertEqual(row["confirmations"], 12)
        self.assertEqual(row["txid"], TXID)

    def test_scan_is_chunked_to_the_endpoint_range_cap(self) -> None:
        """Base's public node refuses eth_getLogs over 500 blocks. A single
        oversized request would fail every sweep after any downtime."""
        fake, calls = self._rpc()
        with patch.object(chain, "_rpc", side_effect=fake):
            chain.scan_inbound_usdc(WALLET, chain_id=BASE,
                                    from_block=LATEST - 1200)
        ranges = [
            (int(p[0]["fromBlock"], 16), int(p[0]["toBlock"], 16))
            for m, p in calls if m == "eth_getLogs"
        ]
        self.assertEqual(len(ranges), 3)
        for lo, hi in ranges:
            self.assertLessEqual(hi - lo + 1, 500)
        self.assertEqual(ranges[0][0], LATEST - 1200)
        self.assertEqual(ranges[-1][1], LATEST)
        # Filtered at the node on the USDC contract and our address as `to`.
        first = calls[1][1][0]
        self.assertEqual(first["address"], chain.USDC_CONTRACTS[BASE])
        self.assertEqual(first["topics"][2], _topic(WALLET))

    def test_only_transfers_into_our_address_count(self) -> None:
        fake, _ = self._rpc([[
            transfer_log(),
            transfer_log(sender=WALLET, recipient=THEIRS, txid="0x" + "b" * 64),
        ]])
        with patch.object(chain, "_rpc", side_effect=fake):
            rows, _ = chain.scan_inbound_usdc(WALLET, chain_id=BASE,
                                              from_block=LATEST - 10)
        self.assertEqual([r["txid"] for r in rows], [TXID])

    def test_recent_inbound_uses_rpc_when_etherscan_does_not_serve_chain(self) -> None:
        """Etherscan's plan refusal is remembered; the next pass goes
        straight to RPC and hands back a cursor to resume from."""
        refusal = {
            "status": "0", "message": "NOTOK",
            "result": "Free API access is not supported for this chain. Please upgrade",
        }
        with patch.object(config, "ETHERSCAN_API_KEY", "k"), \
             patch("chain.requests.get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = refusal
            with self.assertRaises(chain.ChainError):
                chain.usdc_transfers(WALLET, chain_id=BASE)
            self.assertFalse(chain.etherscan_available(BASE))
            self.assertTrue(chain.etherscan_available(1))
            self.assertTrue(chain.readable(BASE))

        fake, calls = self._rpc([[transfer_log()]])
        with patch.object(config, "ETHERSCAN_API_KEY", "k"), \
             patch.object(chain, "_rpc", side_effect=fake), \
             patch.object(chain, "_request",
                          side_effect=AssertionError("etherscan called")):
            rows, cursor = chain.recent_inbound_usdc(WALLET, chain_id=BASE,
                                                     cursor_block=None)
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(cursor)
        self.assertLess(cursor, LATEST)          # resumes a little behind tip
        self.assertGreater(cursor, LATEST - 100)

    def test_recent_inbound_prefers_etherscan_where_it_works(self) -> None:
        with patch.object(config, "ETHERSCAN_API_KEY", "k"), \
             patch.object(chain, "_request", return_value=[transfer()]), \
             patch.object(chain, "_rpc",
                          side_effect=AssertionError("rpc called")):
            rows, cursor = chain.recent_inbound_usdc(OURS, chain_id=1)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(cursor)

    def test_balance_falls_back_to_rpc(self) -> None:
        chain._etherscan_unsupported[BASE] = float("inf")
        fake, _ = self._rpc()
        with patch.object(config, "ETHERSCAN_API_KEY", "k"), \
             patch.object(chain, "_rpc", side_effect=fake):
            self.assertEqual(chain.usdc_balance(WALLET, chain_id=BASE), 1234.56)

    def test_no_source_at_all_raises_rather_than_reading_empty(self) -> None:
        with patch.object(config, "ETHERSCAN_API_KEY", None), \
             patch.object(config, "BASE_RPC_URL", None):
            self.assertFalse(chain.readable(BASE))
            with self.assertRaises(chain.ChainError):
                chain.recent_inbound_usdc(WALLET, chain_id=BASE)


if __name__ == "__main__":
    unittest.main()
