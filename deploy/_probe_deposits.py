"""Read-only: find an endpoint this key can use to SEE an incoming transfer.

Auto-crediting means deciding without a human that money arrived and whose it
is, which rests entirely on what Coinbase will tell us. /api/v2/.../transactions
404s for this CDP key, so this walks the candidates and reports which return
200, how many rows, and whether a row carries the two fields that matter: the
sender address (attribution) and the tx hash (matching a tester's claim).

A zero-row 200 is not a dead end -- the account may simply never have taken an
on-chain USDC deposit -- so shape and reachability are reported separately.

    sudo -u ethagent .venv/bin/python deploy/_probe_deposits.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coinbase_deriv  # noqa: E402

USDC_ACCT = "5ee3b053-01f2-5664-9ef2-807e5d0760e8"


def probe(gw, method: str, path: str, params: dict | None = None):
    try:
        res = gw._request(method, path, params=params or {})
    except Exception as exc:
        first = str(exc).split("\n")[0]
        print(f"  {'FAIL':<5} {path:<62} {first[:70]}")
        return None
    rows = None
    for key in ("data", "transactions", "transfers", "deposits", "fills",
                "portfolios", "accounts", "results"):
        if isinstance(res.get(key), list):
            rows = res[key]
            break
    n = len(rows) if rows is not None else "-"
    print(f"  {'OK':<5} {path:<62} rows={n}")
    return res, rows


def main() -> int:
    gw = coinbase_deriv.get_gateway()

    print("=== candidate history endpoints ===")
    addr_id = None
    res = probe(gw, "GET", f"/api/v2/accounts/{USDC_ACCT}/addresses",
                {"limit": 10})
    if res and res[1]:
        addr_id = res[1][0].get("id")
        print(f"        deposit address id: {addr_id}")

    candidates: list[tuple[str, dict]] = [
        (f"/api/v2/accounts/{USDC_ACCT}/transactions", {"limit": 25}),
        (f"/api/v2/accounts/{USDC_ACCT}/deposits", {"limit": 25}),
        (f"/api/v2/accounts/{USDC_ACCT}/buys", {"limit": 5}),
        ("/api/v2/transactions", {"limit": 25}),
        ("/api/v3/brokerage/transaction_summary", {}),
        ("/api/v3/brokerage/portfolios", {}),
        ("/api/v3/brokerage/orders/historical/fills", {"limit": 5}),
        ("/api/v3/brokerage/cfm/sweeps", {}),
        ("/api/v3/brokerage/payment_methods", {}),
    ]
    if addr_id:
        candidates.insert(
            0,
            (f"/api/v2/accounts/{USDC_ACCT}/addresses/{addr_id}/transactions",
             {"limit": 25}),
        )

    reachable: list[tuple[str, list]] = []
    for path, params in candidates:
        got = probe(gw, "GET", path, params)
        if got and got[1]:
            reachable.append((path, got[1]))

    print("\n=== shape of anything we found ===")
    if not reachable:
        print("  no endpoint returned rows.")
    for path, rows in reachable:
        print(f"\n--- {path} ---")
        row = rows[0]
        print(json.dumps(row, indent=4, default=str)[:2200])
        net = row.get("network") or {}
        print("  sender present:", bool(net.get("from") or row.get("from")))
        print("  hash present:  ",
              bool(net.get("hash") or net.get("transaction_hash")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
