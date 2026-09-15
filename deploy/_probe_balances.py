"""Read-only: enumerate every balance Coinbase reports for this account.

The pool reconciler needs to know what total assets are, and the CFM summary's
own fields do not obviously add up to the spot wallets -- total_usd_balance
(499.98) splits into cbi 431.31 + cfm 68.67, while the spot USDC wallet alone
holds 3,353.17. Guessing a formula for a fiduciary check is how you build one
that silently over-reports coverage, so this dumps every source side by side
to establish which are disjoint before any summing logic is written.

    sudo -u ethagent .venv/bin/python deploy/_probe_balances.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coinbase_deriv  # noqa: E402


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    gw = coinbase_deriv.get_gateway()

    print("=== /api/v3/brokerage/accounts (canonical Advanced Trade balances) ===")
    v3_total = 0.0
    try:
        res = gw._request("GET", "/api/v3/brokerage/accounts", params={"limit": 250})
        for a in res.get("accounts") or []:
            avail = (a.get("available_balance") or {})
            hold = (a.get("hold") or {})
            cur = avail.get("currency") or a.get("currency")
            amt, held = _f(avail.get("value")), _f(hold.get("value"))
            if amt or held:
                print(
                    f"  {str(cur):<6} available={amt:>14,.6f} hold={held:>10,.4f} "
                    f"type={a.get('type')} platform={a.get('platform')} "
                    f"default={a.get('active')}"
                )
                if cur in ("USD", "USDC"):
                    v3_total += amt + held
        print(f"  -> USD+USDC across v3 accounts: ${v3_total:,.2f}")
    except Exception as exc:
        print(f"  failed: {exc}")

    print("\n=== /api/v2/accounts (retail view, incl. wallet vs fiat types) ===")
    v2_total = 0.0
    try:
        res = gw._request("GET", "/api/v2/accounts", params={"limit": 100})
        for a in res.get("data") or []:
            cur = a.get("currency")
            code = cur.get("code") if isinstance(cur, dict) else cur
            amt = _f((a.get("balance") or {}).get("amount"))
            if amt:
                print(
                    f"  {str(code):<6} balance={amt:>14,.6f} type={a.get('type')} "
                    f"primary={a.get('primary')} id={a.get('id')}"
                )
                if code in ("USD", "USDC"):
                    v2_total += amt
        print(f"  -> USD+USDC across v2 accounts: ${v2_total:,.2f}")
    except Exception as exc:
        print(f"  failed: {exc}")

    print("\n=== CFM futures balance summary (what reconcile reads today) ===")
    try:
        raw = gw.get_account_summary().get("raw") or {}
        for key in (
            "total_usd_balance", "cbi_usd_balance", "cfm_usd_balance",
            "futures_buying_power", "initial_margin", "available_margin",
            "total_open_orders_hold_amount", "total_pending_transfers_amount",
            "unrealized_pnl",
        ):
            val = raw.get(key)
            if isinstance(val, dict):
                val = val.get("value")
            print(f"  {key:<34} {val}")
    except Exception as exc:
        print(f"  failed: {exc}")

    print("\n=== does anything double count? ===")
    print(f"  v3 USD+USDC total        ${v3_total:,.2f}")
    print(f"  v2 USD+USDC total        ${v2_total:,.2f}")
    print("  If v3 and v2 agree, they are two views of ONE set of spot wallets,")
    print("  and cfm_usd_balance is the only genuinely separate futures pot.")

    print("\n=== raw CFM dump for the record ===")
    try:
        print(json.dumps(gw.get_account_summary().get("raw"), indent=2)[:2000])
    except Exception as exc:
        print(f"  failed: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
