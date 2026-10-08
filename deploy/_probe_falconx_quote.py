"""Probe FalconX quote endpoints (v1 and v3) forced to IPv4.

Narrows down whether REQUEST_IP_RESTRICTED on /v3/quotes is specific to the
v3 service, while v1 reads succeed from the same whitelisted IPv4.
"""

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
    sig = base64.b64encode(hmac.new(key, msg.encode(), hashlib.sha256).digest()).decode()
    headers = {
        "FX-ACCESS-KEY": os.environ["FALCONX_API_KEY"],
        "FX-ACCESS-PASSPHRASE": os.environ["FALCONX_PASSPHRASE"],
        "FX-ACCESS-TIMESTAMP": ts,
        "FX-ACCESS-SIGN": sig,
        "Content-Type": "application/json",
    }
    resp = requests.request(method, API + path, headers=headers, data=payload or None, timeout=20)
    print(f"\n== {method} {path} -> {resp.status_code}")
    print(resp.text[:800])


QUOTE = {
    "token_pair": {"base_token": "ETH", "quote_token": "USD"},
    "quantity": {"token": "ETH", "value": "0.05"},
    "side": "two_way",
}

call("POST", "/v1/quotes", QUOTE)
call("POST", "/v3/quotes", QUOTE)
