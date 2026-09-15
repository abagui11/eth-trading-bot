"""Read-only: isolate a 401 — is it the payout code, or the new key?

The payout client and the trading gateway build the same JWT claims, and the
trading key works. So running the TRADING key through the PAYOUT code splits
the question cleanly: if that succeeds, the code is fine and the Ed25519 key
or its state is the problem; if it fails too, the payout client is at fault.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import payouts  # noqa: E402

PATH = "/api/v3/brokerage/key_permissions"


def attempt(label: str, name: str | None, key: str | None) -> None:
    if not name or not key:
        print(f"{label}: (not configured)")
        return
    with patch.object(config, "COINBASE_TRANSFER_KEY_NAME", name), \
         patch.object(config, "COINBASE_TRANSFER_PRIVATE_KEY", key):
        try:
            _, alg = payouts.signing_key(key)
        except Exception as exc:
            print(f"{label}: key format rejected — {exc}")
            return
        try:
            res = payouts._request("GET", PATH)
            print(f"{label} [{alg}]: OK — {res}")
        except Exception as exc:
            print(f"{label} [{alg}]: {str(exc)[:160]}")


print("== payout code, TRADING key (ECDSA) ==")
attempt("trading", config.COINBASE_CDP_API_KEY_NAME, config.COINBASE_CDP_PRIVATE_KEY)

print("\n== payout code, TRANSFER key (Ed25519) ==")
attempt("transfer", config.COINBASE_TRANSFER_KEY_NAME,
        config.COINBASE_TRANSFER_PRIVATE_KEY)

print("\n== what got stored ==")
raw = config.COINBASE_TRANSFER_PRIVATE_KEY or ""
print("stored length      :", len(raw))
print("looks base64       :", "BEGIN" not in raw)
try:
    import base64
    decoded = base64.b64decode(raw.strip(), validate=True)
    print("decoded bytes      :", len(decoded), "(expect 64: seed||public)")
except Exception as exc:
    print("decode failed      :", exc)
name = config.COINBASE_TRANSFER_KEY_NAME or ""
print("name has org prefix:", name.startswith("organizations/"))
print("name segments      :", len(name.split("/")))
