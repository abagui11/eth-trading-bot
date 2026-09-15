"""Read-only: do both keys belong to the same Coinbase organization?

A key issued under a different org (or a different key product) authenticates
against a different tenant, which looks exactly like the uniform 401 we are
seeing: correct signature, unknown identity.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def org(name: str | None) -> str:
    if not name or "/" not in name:
        return "(unset)"
    parts = name.split("/")
    return parts[1] if len(parts) > 1 else "(none)"


def kid(name: str | None) -> str:
    if not name:
        return "(unset)"
    k = name.rsplit("/", 1)[-1]
    return f"{k[:4]}***{k[-4:]}"


t_org = org(config.COINBASE_CDP_API_KEY_NAME)
x_org = org(config.COINBASE_TRANSFER_KEY_NAME)

print(f"trading  {kid(config.COINBASE_CDP_API_KEY_NAME)}  org {t_org[:8]}...")
print(f"transfer {kid(config.COINBASE_TRANSFER_KEY_NAME)}  org {x_org[:8]}...")
print()
print("same organization:", t_org == x_org)
if t_org != x_org:
    print("\n-> The new key was issued under a different org than the working")
    print("   key. That alone produces a 401 on every request, whatever the")
    print("   signature looks like. Create the key from the same place that")
    print("   issued the trading key.")
