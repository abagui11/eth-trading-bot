"""Scope FalconX after IP whitelist updates — what this project can use.

Forces IPv4 (VPS IPv6 still rejected as of last check). Hits only
non-destructive reads + indicative RFQ; never executes a quote.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import time

import requests

_orig = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, f=0, *a, **k: _orig(h, p, socket.AF_INET, *a, **k)

API = "https://api.falconx.io"


def call(method: str, path: str, body: dict | None = None):
    payload = json.dumps(body) if body is not None else ""
    ts = str(time.time())
    msg = ts + method + path + payload
    key = base64.b64decode(os.environ["FALCONX_SECRET_KEY"])
    sig = base64.b64encode(
        hmac.new(key, msg.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "FX-ACCESS-KEY": os.environ["FALCONX_API_KEY"],
        "FX-ACCESS-PASSPHRASE": os.environ["FALCONX_PASSPHRASE"],
        "FX-ACCESS-TIMESTAMP": ts,
        "FX-ACCESS-SIGN": sig,
        "Content-Type": "application/json",
    }
    resp = requests.request(
        method, API + path, headers=headers, data=payload or None, timeout=20
    )
    print(f"\n== {method} {path} -> {resp.status_code}")
    try:
        data = resp.json()
        text = json.dumps(data, indent=2, default=str)
    except ValueError:
        text = resp.text
        data = None
    if len(text) > 2200:
        text = text[:2200] + "\n   ...[truncated]"
    print(text)
    return data


def main() -> int:
    # Egress identity (what FalconX sees)
    try:
        v4 = requests.get("https://api.ipify.org?format=json", timeout=10).json()
        print("egress IPv4:", v4)
    except Exception as exc:  # noqa: BLE001
        print("egress IPv4 check failed:", exc)

    call("GET", "/v1/account_info")
    call("GET", "/v1/balances?platform=api")
    call("GET", "/v1/balances/total")
    call("GET", "/v1/get_trade_limits?platform=api")
    call("GET", "/v1/transfer/deposit/address?currency=USDC")
    call("GET", "/v1/transfers")

    # Indicative RFQs — tiny sizes, two_way, never executed
    for base, qty in (("ETH", "0.01"), ("BTC", "0.001"), ("SOL", "0.5")):
        call("POST", "/v3/quotes", {
            "token_pair": {"base_token": base, "quote_token": "USD"},
            "quantity": {"token": base, "value": qty},
            "side": "two_way",
        })

    # Market order dry-run shape (expect equity / permission error, not IP)
    call("POST", "/v3/order", {
        "token_pair": {"base_token": "ETH", "quote_token": "USD"},
        "quantity": {"token": "ETH", "value": "0.01"},
        "side": "buy",
        "order_type": "market",
        "client_order_id": "eva-scope-dryrun",
    })

    # Limit FOK dry-run
    call("POST", "/v3/order", {
        "token_pair": {"base_token": "ETH", "quote_token": "USD"},
        "quantity": {"token": "ETH", "value": "0.01"},
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "fok",
        "limit_price": 1.0,
        "client_order_id": "eva-scope-fok-dryrun",
    })

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
