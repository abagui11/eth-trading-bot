"""Payout client — the transfer-only path off the venue.

Signing is covered for both key formats the CDP portal issues, because the
portal defaults to Ed25519 while the existing trading key is ECDSA, and a
signature that only works for one of them fails at the worst possible moment:
against a live withdrawal.
"""

from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

import config
import payouts

KEY_NAME = "organizations/org/apiKeys/abcd1234"


def ecdsa_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def ed25519_b64(*, with_public_half: bool = True) -> str:
    """CDP hands out base64 of seed||public (64 bytes)."""
    key = ed25519.Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(seed + pub if with_public_half else seed).decode()


class SigningKeyTests(unittest.TestCase):
    def test_a_pem_signs_es256(self) -> None:
        key, alg = payouts.signing_key(ecdsa_pem())
        self.assertEqual(alg, "ES256")
        self.assertIn("BEGIN", key)

    def test_escaped_newlines_are_restored(self) -> None:
        """.env stores the PEM on one line with literal \\n, the way systemd
        leaves it; an unrestored key will not parse."""
        key, alg = payouts.signing_key(ecdsa_pem().replace("\n", "\\n"))
        self.assertEqual(alg, "ES256")
        self.assertIn("-----BEGIN", key)
        self.assertNotIn("\\n", key)

    def test_a_cdp_ed25519_key_signs_eddsa(self) -> None:
        key, alg = payouts.signing_key(ed25519_b64())
        self.assertEqual(alg, "EdDSA")
        self.assertIsInstance(key, ed25519.Ed25519PrivateKey)

    def test_only_the_seed_half_is_used_as_the_private_key(self) -> None:
        """CDP concatenates the public half onto the seed. Feeding all 64
        bytes to the signer is the classic way this goes wrong."""
        raw = base64.b64decode(ed25519_b64())
        self.assertEqual(len(raw), 64)
        key, _ = payouts.signing_key(base64.b64encode(raw).decode())
        expected = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
        self.assertEqual(
            key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            expected.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )

    def test_a_bare_32_byte_seed_also_works(self) -> None:
        key, alg = payouts.signing_key(ed25519_b64(with_public_half=False))
        self.assertEqual(alg, "EdDSA")

    def test_garbage_is_refused_rather_than_signed_wrongly(self) -> None:
        with self.assertRaises(payouts.PayoutError):
            payouts.signing_key("not a key at all !!!")
        with self.assertRaises(payouts.PayoutError):
            payouts.signing_key(base64.b64encode(b"tooshort").decode())


class JwtTests(unittest.TestCase):
    """The JWT must verify against the matching public key, for both formats."""

    def _roundtrip(self, private_key: str, algorithm: str, pub) -> None:
        with patch.object(config, "COINBASE_TRANSFER_KEY_NAME", KEY_NAME), \
             patch.object(config, "COINBASE_TRANSFER_PRIVATE_KEY", private_key):
            token = payouts._build_jwt("POST", "/api/v2/accounts/x/transactions")
        claims = pyjwt.decode(
            token, pub, algorithms=[algorithm],
            options={"verify_aud": False},
        )
        self.assertEqual(claims["sub"], KEY_NAME)
        self.assertEqual(claims["iss"], "cdp")
        # uri-bound: a token lifted from one call cannot be replayed on another
        self.assertEqual(
            claims["uri"], "POST api.coinbase.com/api/v2/accounts/x/transactions"
        )
        self.assertLessEqual(claims["exp"] - claims["nbf"], 120)

    def test_ecdsa_jwt_verifies(self) -> None:
        pem = ecdsa_pem()
        pub = serialization.load_pem_private_key(pem.encode(), password=None)
        self._roundtrip(pem, "ES256", pub.public_key())

    def test_ed25519_jwt_verifies(self) -> None:
        b64 = ed25519_b64()
        raw = base64.b64decode(b64)
        pub = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32]).public_key()
        self._roundtrip(b64, "EdDSA", pub)

    def test_a_missing_key_says_what_to_do(self) -> None:
        with patch.object(config, "COINBASE_TRANSFER_KEY_NAME", None), \
             patch.object(config, "COINBASE_TRANSFER_PRIVATE_KEY", None):
            with self.assertRaises(payouts.PayoutError) as ctx:
                payouts._build_jwt("GET", "/x")
        self.assertIn("View + Transfer", str(ctx.exception))


class SendGuardTests(unittest.TestCase):
    def test_a_non_positive_send_is_refused_before_any_network_call(self) -> None:
        for amount in (0, -1, -0.01):
            with self.assertRaises(payouts.PayoutError):
                payouts.send(account_id="a", to_address="0x1",
                             amount_usd=amount, idem="i")

    def test_the_amount_goes_out_as_a_fixed_string(self) -> None:
        """Floats are not safe to hand a payments API, and the rounding error
        here would be somebody's money."""
        seen: dict = {}

        def fake(method, path, *, params=None, body=None):
            seen.update(body or {})
            return {"data": {"id": "t1", "status": "pending",
                             "amount": {"amount": "10.00"}}}

        with patch.object(payouts, "_request", fake):
            payouts.send(account_id="a", to_address="0xdead",
                         amount_usd=10.005, idem="req:42")

        self.assertEqual(seen["amount"], "10.01")
        self.assertIsInstance(seen["amount"], str)
        self.assertEqual(seen["idem"], payouts.idem_uuid("req:42"))
        self.assertEqual(seen["type"], "send")
        self.assertEqual(seen["currency"], "USDC")


class IdemTests(unittest.TestCase):
    """Coinbase refuses a non-UUID idem key, and the obvious fix for that --
    a fresh uuid4 per attempt -- would satisfy the format while silently
    removing the double-pay protection the key exists for."""

    def test_the_derived_key_is_a_valid_uuid(self) -> None:
        import uuid as _uuid

        self.assertEqual(
            str(_uuid.UUID(payouts.idem_uuid("withdrawal_request:42"))),
            payouts.idem_uuid("withdrawal_request:42"),
        )

    def test_the_same_withdrawal_always_maps_to_the_same_key(self) -> None:
        """Stability across restarts is the whole point; a retry must carry
        the key the first attempt used."""
        self.assertEqual(
            payouts.idem_uuid("withdrawal_request:42"),
            payouts.idem_uuid("withdrawal_request:42"),
        )

    def test_different_withdrawals_get_different_keys(self) -> None:
        self.assertNotEqual(
            payouts.idem_uuid("withdrawal_request:42"),
            payouts.idem_uuid("withdrawal_request:43"),
        )

    def test_a_key_that_is_already_a_uuid_passes_through(self) -> None:
        given = "0b2b4b1e-6a3c-4f1e-9b2a-1c2d3e4f5a6b"
        self.assertEqual(payouts.idem_uuid(given), given)

    def test_the_mapping_is_pinned_so_it_cannot_drift(self) -> None:
        """If the namespace ever changed, every in-flight retry would present
        a new key and could pay twice. This fails loudly if that happens."""
        self.assertEqual(
            payouts.idem_uuid("withdrawal_request:1"),
            "13aead37-0af6-57fb-9679-1da2b94e750b",
        )

    def test_a_timeout_on_send_is_marked_submitted(self) -> None:
        """The request reached Coinbase and the outcome is unknown, so the
        caller must reconcile. A blind retry here pays twice."""
        import requests

        with patch.object(payouts.requests, "request",
                          side_effect=requests.Timeout("boom")):
            with patch.object(payouts, "_build_jwt", return_value="t"):
                with self.assertRaises(payouts.PayoutError) as ctx:
                    payouts._request("POST", "/api/v2/accounts/a/transactions")
        self.assertTrue(ctx.exception.submitted)

    def test_a_timeout_on_a_read_is_not_marked_submitted(self) -> None:
        import requests

        with patch.object(payouts.requests, "request",
                          side_effect=requests.Timeout("boom")):
            with patch.object(payouts, "_build_jwt", return_value="t"):
                with self.assertRaises(payouts.PayoutError) as ctx:
                    payouts._request("GET", "/api/v2/accounts")
        self.assertFalse(ctx.exception.submitted)


if __name__ == "__main__":
    unittest.main()
