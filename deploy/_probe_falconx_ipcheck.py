"""Quick FalconX auth check: default routing vs forced IPv4.

Usage (on the VPS):
    set -a; . /opt/eth-trading-agent/secrets/falconx.env; set +a
    python deploy/_probe_falconx_ipcheck.py          # default (IPv6 if available)
    python deploy/_probe_falconx_ipcheck.py --ipv4   # force IPv4
"""

import base64
import hashlib
import hmac
import os
import socket
import sys
import time

import requests

if "--ipv4" in sys.argv:
    _orig = socket.getaddrinfo

    def _v4only(host, port, family=0, *a, **k):
        return _orig(host, port, socket.AF_INET, *a, **k)

    socket.getaddrinfo = _v4only
    print("[forced IPv4]")
else:
    print("[default address family]")

ts = str(time.time())
path = "/v1/account_info"
msg = ts + "GET" + path
key = base64.b64decode(os.environ["FALCONX_SECRET_KEY"])
sig = base64.b64encode(hmac.new(key, msg.encode(), hashlib.sha256).digest()).decode()
headers = {
    "FX-ACCESS-KEY": os.environ["FALCONX_API_KEY"],
    "FX-ACCESS-PASSPHRASE": os.environ["FALCONX_PASSPHRASE"],
    "FX-ACCESS-TIMESTAMP": ts,
    "FX-ACCESS-SIGN": sig,
}
try:
    r = requests.get("https://api.falconx.io" + path, headers=headers, timeout=20)
except Exception as exc:  # noqa: BLE001
    print("EXC:", exc)
    sys.exit(1)
print("status:", r.status_code)
print(r.text[:600])
