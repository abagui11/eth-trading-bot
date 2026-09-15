"""Read-only probe: what can the live Coinbase CDP key actually do?

Answers whether withdrawals are reachable at all with the current credential,
before any withdrawal design is built on the assumption that they are. Makes
only GET calls -- nothing is moved, created, or cancelled.

    sudo -u ethagent .venv/bin/python deploy/_check_transfer_scope.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coinbase_deriv  # noqa: E402


def main() -> int:
    gw = coinbase_deriv.get_gateway()

    print("=== key permissions ===")
    try:
        perms = gw._request("GET", "/api/v3/brokerage/key_permissions")
        for k, v in sorted(perms.items()):
            print(f"  {k:<20} {v}")
        if perms.get("can_transfer"):
            print("\n  -> Transfer scope IS present: API withdrawals are reachable.")
        else:
            print(
                "\n  -> Transfer scope is ABSENT. API withdrawals need a new CDP "
                "key with Transfer enabled; this key cannot move funds out."
            )
    except Exception as exc:
        print(f"  failed: {exc}")

    print("\n=== v2 accounts reachable with this key? (deposit-address surface) ===")
    try:
        accounts = gw._request("GET", "/api/v2/accounts", params={"limit": 100})
        rows = accounts.get("data") or []
        print(f"  {len(rows)} account(s) visible")
        for a in rows:
            cur = (a.get("currency") or {})
            code = cur.get("code") if isinstance(cur, dict) else cur
            bal = (a.get("balance") or {}).get("amount")
            if code in ("USDC", "USD") or (bal and float(bal) > 0):
                print(f"    {str(code):<6} balance={bal} id={a.get('id')} type={a.get('type')}")
    except Exception as exc:
        print(f"  failed: {exc}")

    print("\n=== derivatives collateral (what the pool's claims are backed by) ===")
    try:
        summary = gw.get_account_summary()
        for k, v in sorted(summary.items()):
            print(f"  {k:<24} {v}")
    except Exception as exc:
        print(f"  failed: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
