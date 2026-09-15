"""Read-only: confirm a deposit address really belongs to our Coinbase account.

The failure this guards against has no undo. If POOL_DEPOSIT_ADDRESS carries a
typo, or names an address on a network Coinbase does not credit, testers send
USDC to somewhere nobody controls and it is gone. Coinbase lists the deposit
addresses it generated for an account, so the configured value can be checked
against that list instead of trusted.

    sudo -u ethagent .venv/bin/python deploy/_check_deposit_address.py [0xaddr]
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import coinbase_deriv  # noqa: E402


def main() -> int:
    want = (sys.argv[1] if len(sys.argv) > 1 else config.POOL_DEPOSIT_ADDRESS or "")
    want = want.strip().lower()
    if not want:
        print("no address given and POOL_DEPOSIT_ADDRESS is unset")
        return 2

    gw = coinbase_deriv.get_gateway()
    accounts = gw._request("GET", "/api/v2/accounts", params={"limit": 100})

    found: list[tuple[str, str, str]] = []
    for acct in accounts.get("data") or []:
        cur = acct.get("currency")
        code = cur.get("code") if isinstance(cur, dict) else cur
        acct_id = acct.get("id")
        if not acct_id:
            continue
        try:
            res = gw._request(
                "GET", f"/api/v2/accounts/{acct_id}/addresses",
                params={"limit": 100},
            )
        except Exception as exc:
            print(f"  {code}: address list unavailable ({exc})")
            continue
        for addr in res.get("data") or []:
            value = str(addr.get("address") or "")
            network = str(addr.get("network") or "")
            label = str(addr.get("name") or addr.get("label") or "")
            found.append((str(code), value, network))
            print(f"  {str(code):<6} {value}  network={network:<20} {label}")

    print(f"\nlooking for: {want}")
    hits = [f for f in found if f[1].strip().lower() == want]
    if hits:
        code, value, network = hits[0]
        print(f"MATCH — belongs to the {code} account on network '{network}'")
        print("Funds sent here credit the Coinbase account directly.")
        return 0

    print("NO MATCH among the deposit addresses Coinbase reports for this key.")
    print("That is not proof it is wrong — the address may predate the key, or")
    print("live under an account this key cannot enumerate. Confirm it in the")
    print("Coinbase UI before any tester sends to it.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
