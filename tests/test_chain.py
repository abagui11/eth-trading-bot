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


if __name__ == "__main__":
    unittest.main()
