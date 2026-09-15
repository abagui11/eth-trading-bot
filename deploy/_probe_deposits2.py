"""Read-only: every row on the deposit address, in full.

The watcher has to tell an inbound transfer from an outbound one and a settled
one from an in-flight one, using only these fields. Getting that filter wrong
credits a tester for money that left, or for money that has not arrived, so
both rows are dumped whole rather than sampled.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coinbase_deriv  # noqa: E402

ACCT = "5ee3b053-01f2-5664-9ef2-807e5d0760e8"
ADDR = "8f3d419d-aaac-5003-9a30-a76847d3659d"

gw = coinbase_deriv.get_gateway()
res = gw._request(
    "GET", f"/api/v2/accounts/{ACCT}/addresses/{ADDR}/transactions",
    params={"limit": 100},
)

print("top-level keys:", sorted(res.keys()))
print("pagination:", json.dumps(res.get("pagination"), indent=2, default=str))
rows = res.get("data") or []
print(f"\n{len(rows)} row(s)\n")
for i, row in enumerate(rows):
    print("=" * 70)
    print(f"row {i}: all keys = {sorted(row.keys())}")
    print(json.dumps(row, indent=2, default=str))
