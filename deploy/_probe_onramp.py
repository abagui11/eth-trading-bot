"""Probe: can our existing CDP keys mint a Coinbase Onramp session token?

Creates a single-use token pinned to TEST_WALLET_ADDRESS on Base. Moves no
money — the token expires in 5 minutes unused. Prints status only.
"""

from __future__ import annotations

import secrets
import time

import jwt as pyjwt
import requests

import config

HOST = "api.developer.coinbase.com"
PATH = "/onramp/v1/token"


def _jwt(key_name: str, private_key: str) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120,
         "uri": f"POST {HOST}{PATH}"},
        private_key.replace("\\n", "\n"),
        algorithm="ES256",
        headers={"kid": key_name, "nonce": secrets.token_hex(16)},
    )


def probe(label: str, key_name: str | None, private_key: str | None) -> None:
    if not key_name or not private_key:
        print(label, "missing")
        return
    body = {
        "addresses": [{"address": config.TEST_WALLET_ADDRESS,
                       "blockchains": ["base"]}],
        "assets": ["USDC"],
        "clientIp": "192.0.2.1",
    }
    try:
        res = requests.post(
            f"https://{HOST}{PATH}",
            headers={"Authorization": f"Bearer {_jwt(key_name, private_key)}",
                     "Content-Type": "application/json"},
            json=body, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        print(label, "transport_error", exc)
        return
    ok = res.status_code == 200 and "token" in res.text
    print(label, res.status_code, "TOKEN_OK" if ok else res.text[:300])


if __name__ == "__main__":
    print("wallet", config.TEST_WALLET_ADDRESS)
    probe("trading_key", config.COINBASE_CDP_API_KEY_NAME,
          config.COINBASE_CDP_PRIVATE_KEY)
    probe("transfer_key", getattr(config, "COINBASE_TRANSFER_KEY_NAME", None),
          getattr(config, "COINBASE_TRANSFER_PRIVATE_KEY", None))
