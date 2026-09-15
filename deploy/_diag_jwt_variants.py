"""Read-only: try the plausible Ed25519 JWT constructions against Coinbase.

The claim structure is proven for ECDSA, so if Ed25519 needs a different one
it is a small, enumerable difference: the issuer string, whether the key id is
the full resource name or the bare uuid, the nonce header, or a legacy `aud`
instead of the uri binding. Cheaper to test all of them than to guess, and a
GET on key_permissions is harmless to repeat.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import time

import jwt as pyjwt
import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

HOST = "api.coinbase.com"
PATH = "/api/v3/brokerage/key_permissions"

name = config.COINBASE_TRANSFER_KEY_NAME or ""
secret = (config.COINBASE_TRANSFER_PRIVATE_KEY or "").strip()
uuid_only = name.rsplit("/", 1)[-1]
raw = base64.b64decode(secret, validate=True)


def try_jwt(label: str, claims: dict, headers: dict, seed: bytes) -> None:
    key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    token = pyjwt.encode(claims, key, algorithm="EdDSA", headers=headers)
    res = requests.get(
        f"https://{HOST}{PATH}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    flag = "  <-- WORKS" if res.status_code == 200 else ""
    print(f"  {res.status_code}  {label}{flag}")
    if res.status_code == 200:
        print("       ", res.text[:200])


now = int(time.time())
base = {"sub": name, "iss": "cdp", "nbf": now, "exp": now + 120,
        "uri": f"GET {HOST}{PATH}"}

print(f"key {uuid_only[:4]}***{uuid_only[-4:]}, secret {len(raw)} bytes\n")

variants = [
    ("current: sub=name, iss=cdp, uri, nonce, seed[:32]",
     dict(base), {"kid": name, "nonce": secrets.token_hex(16)}, raw[:32]),
    ("no nonce header",
     dict(base), {"kid": name}, raw[:32]),
    ("kid = bare uuid",
     dict(base), {"kid": uuid_only, "nonce": secrets.token_hex(16)}, raw[:32]),
    ("sub = bare uuid",
     {**base, "sub": uuid_only}, {"kid": name, "nonce": secrets.token_hex(16)},
     raw[:32]),
    ("iss = coinbase-cloud",
     {**base, "iss": "coinbase-cloud"},
     {"kid": name, "nonce": secrets.token_hex(16)}, raw[:32]),
    ("legacy aud instead of uri",
     {k: v for k, v in base.items() if k != "uri"}
     | {"aud": ["retail_rest_api_proxy"]},
     {"kid": name, "nonce": secrets.token_hex(16)}, raw[:32]),
    ("uri without method",
     {**base, "uri": f"{HOST}{PATH}"},
     {"kid": name, "nonce": secrets.token_hex(16)}, raw[:32]),
]
if len(raw) == 64:
    variants.append(
        ("seed = LAST 32 bytes (if order were reversed)",
         dict(base), {"kid": name, "nonce": secrets.token_hex(16)}, raw[32:])
    )

for label, claims, headers, seed in variants:
    try:
        try_jwt(label, claims, headers, seed)
    except Exception as exc:
        print(f"  ---  {label}: {str(exc)[:90]}")
