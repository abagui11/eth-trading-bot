"""Dump FalconX USD/USDC pair coverage for Eva majors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import time

import requests

_orig = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, f=0, *a, **k: _orig(h, p, socket.AF_INET, *a, **k)

API = "https://api.falconx.io"
WANTED = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "LINK", "ADA", "DOT",
    "MATIC", "POL", "ARB", "OP", "NEAR", "APT", "SUI", "PEPE", "SHIB",
    "WIF", "BONK", "UNI", "AAVE", "LTC", "BCH", "ATOM",
]


def main() -> int:
    ts = str(time.time())
    path = "/v1/pairs"
    msg = ts + "GET" + path
    key = base64.b64decode(os.environ["FALCONX_SECRET_KEY"])
    sig = base64.b64encode(hmac.new(key, msg.encode(), hashlib.sha256).digest()).decode()
    headers = {
        "FX-ACCESS-KEY": os.environ["FALCONX_API_KEY"],
        "FX-ACCESS-PASSPHRASE": os.environ["FALCONX_PASSPHRASE"],
        "FX-ACCESS-TIMESTAMP": ts,
        "FX-ACCESS-SIGN": sig,
    }
    pairs = requests.get(API + path, headers=headers, timeout=20).json()
    bases = sorted(
        {p["base_token"] for p in pairs if p.get("quote_token") in ("USD", "USDC")}
    )
    print(f"USD/USDC bases ({len(bases)}): {', '.join(bases)}")
    print("coverage vs Eva majors:")
    for w in WANTED:
        ok = any(
            p["base_token"] == w and p["quote_token"] in ("USD", "USDC")
            for p in pairs
        )
        print(f"  {w}: {'yes' if ok else 'NO'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
