"""Probe the FalconX REST API with a read-only key.

Reads FALCONX_API_KEY / FALCONX_SECRET_KEY / FALCONX_PASSPHRASE from the
environment (source /opt/eth-trading-agent/secrets/falconx.env first).
Hits only read endpoints: account info, pairs, balances, portfolio,
limits, transfers, orders, and an indicative two-way RFQ quote.

    set -a; . /opt/eth-trading-agent/secrets/falconx.env; set +a
    /opt/eth-trading-agent/.venv/bin/python deploy/_probe_falconx.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import requests

API = "https://api.falconx.io"


def _headers(method: str, path: str, body: str = "") -> dict:
    ts = str(time.time())
    message = ts + method + path + body
    key = base64.b64decode(os.environ["FALCONX_SECRET_KEY"])
    sig = base64.b64encode(
        hmac.new(key, message.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "FX-ACCESS-KEY": os.environ["FALCONX_API_KEY"],
        "FX-ACCESS-PASSPHRASE": os.environ["FALCONX_PASSPHRASE"],
        "FX-ACCESS-TIMESTAMP": ts,
        "FX-ACCESS-SIGN": sig,
        "Content-Type": "application/json",
    }


def call(method: str, path: str, body: dict | None = None):
    payload = json.dumps(body) if body is not None else ""
    url = API + path
    try:
        resp = requests.request(
            method, url, headers=_headers(method, path, payload),
            data=payload or None, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n== {method} {path}\n   EXC: {exc}")
        return None
    print(f"\n== {method} {path} -> {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        print("   non-JSON:", resp.text[:300])
        return None
    text = json.dumps(data, indent=2, default=str)
    if len(text) > 2500:
        text = text[:2500] + "\n   ...[truncated]"
    print(text)
    return data


def main() -> int:
    call("GET", "/v1/account_info")
    pairs = call("GET", "/v1/pairs")
    if isinstance(pairs, list):
        print(f"   ({len(pairs)} pairs total)")
        wanted = [
            p for p in pairs
            if isinstance(p, dict)
            and p.get("base_token") in ("BTC", "ETH", "SOL", "XRP", "DOGE")
            and p.get("quote_token") in ("USD", "USDC")
        ]
        print("   relevant:", json.dumps(wanted, default=str)[:1500])
    call("GET", "/v1/balances?platform=api")
    call("GET", "/v1/balances/total")
    call("GET", "/v1/portfolio_balance_details")
    call("GET", "/v1/get_trade_limits?platform=api")
    call("GET", "/v1/get_30_day_trailing_volume")
    call("GET", "/v1/orders")
    call("GET", "/v1/transfers")
    call("GET", "/v1/transfer/deposit/address?currency=USDC")
    call("GET", "/v1/derivatives")
    # Indicative RFQ: a two-way quote, never executed (and this key cannot
    # execute anyway — Execute Trades is unchecked).
    call("POST", "/v3/quotes", {
        "token_pair": {"base_token": "ETH", "quote_token": "USD"},
        "quantity": {"token": "ETH", "value": "0.05"},
        "side": "two_way",
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
