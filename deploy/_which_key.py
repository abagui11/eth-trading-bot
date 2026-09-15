"""Read-only: which CDP key is live, masked so it is safe to read aloud."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def mask(name: str | None) -> str:
    if not name:
        return "(unset)"
    kid = name.rsplit("/", 1)[-1]
    return f"{kid[:4]}***{kid[-4:]}" if len(kid) > 8 else "(short)"


def keytype(pem: str | None) -> str:
    if not pem:
        return "(unset)"
    if "EC PRIVATE KEY" in pem:
        return "ECDSA (SEC1 PEM) - signs ES256"
    if "BEGIN PRIVATE KEY" in pem:
        return "PKCS8 PEM"
    return "raw base64 - probably Ed25519"


print("trading key id   :", mask(config.COINBASE_CDP_API_KEY_NAME))
print("trading key type :", keytype(config.COINBASE_CDP_PRIVATE_KEY))
print("transfer key id  :", mask(getattr(config, "COINBASE_TRANSFER_KEY_NAME", None)))

try:
    from coinbase_deriv import get_gateway

    perms = get_gateway()._request("GET", "/api/v3/brokerage/key_permissions")
    print("\nlive permissions :", {
        k: v for k, v in perms.items()
        if k in ("can_view", "can_trade", "can_transfer", "portfolio_type")
    })
except Exception as exc:
    print("\ncould not read permissions:", str(exc)[:120])
