"""Run deploy/_probe_falconx.py with all connections forced to IPv4.

Needed because the FalconX key is whitelisted for the VPS IPv4 only, and
api.falconx.io prefers IPv6 when the host has a public IPv6 address.
"""

import runpy
import socket

_orig = socket.getaddrinfo


def _v4only(host, port, family=0, *a, **k):
    return _orig(host, port, socket.AF_INET, *a, **k)


socket.getaddrinfo = _v4only

runpy.run_path("deploy/_probe_falconx.py", run_name="__main__")
