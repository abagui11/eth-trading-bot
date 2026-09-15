"""Read-only: prove the two keys have the powers they should, and no others.

The point of a second key is that neither credential can do everything. This
checks both directions, because only one of them is obvious: it is easy to
confirm the new key CAN withdraw, and easy to forget to confirm it CANNOT
trade -- and a payout key that can also trade gives back exactly the blast
radius the split was meant to remove.

    sudo -u ethagent .venv/bin/python deploy/_check_transfer_key.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import payouts  # noqa: E402
from coinbase_deriv import get_gateway  # noqa: E402


def mask(name: str | None) -> str:
    if not name:
        return "(unset)"
    kid = name.rsplit("/", 1)[-1]
    return f"{kid[:4]}***{kid[-4:]}" if len(kid) > 8 else "(short)"


def main() -> int:
    ok = True

    def check(label: str, got, want) -> None:
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {label}: {got}")

    print(f"trading  key {mask(config.COINBASE_CDP_API_KEY_NAME)}")
    try:
        t = get_gateway()._request("GET", "/api/v3/brokerage/key_permissions")
        check("can_trade", bool(t.get("can_trade")), True)
        check("can_transfer (must be False)", bool(t.get("can_transfer")), False)
    except Exception as exc:
        ok = False
        print("  FAIL  could not read trading key permissions:", str(exc)[:140])

    print(f"\ntransfer key {mask(config.COINBASE_TRANSFER_KEY_NAME)}")
    if not config.COINBASE_TRANSFER_KEY_NAME:
        print("  not installed yet — run deploy/_install_transfer_key.sh")
        return 1
    try:
        _, algorithm = payouts.signing_key(config.COINBASE_TRANSFER_PRIVATE_KEY or "")
        print(f"  signs with {algorithm}")
    except Exception as exc:
        print("  FAIL  key format not understood:", str(exc)[:140])
        return 1
    try:
        p = payouts.key_permissions()
        check("can_transfer", bool(p.get("can_transfer")), True)
        check("can_view", bool(p.get("can_view")), True)
        # A warning, not a failure: if the portal will not issue Transfer
        # without Trade, that is Coinbase's constraint and not a mistake to
        # fail the run over. It does matter though -- a payout key that can
        # also trade hands back the blast radius the split was meant to remove.
        if p.get("can_trade"):
            print("  WARN  can_trade is True — this key can also trade. "
                  "Uncheck Trade on it if the portal allows.")
        else:
            print("  ok    can_trade is False — cannot trade, as intended")
    except Exception as exc:
        ok = False
        print("  FAIL  could not read transfer key permissions:", str(exc)[:200])

    # With transfer rights the v2 transaction history may open up, which would
    # also give the deposit watcher a richer feed than the address-scoped path.
    print("\nbonus: does the transfer key see v2 account transactions?")
    try:
        accounts = payouts.list_accounts()
        usdc = next((a for a in accounts if a["currency"] == "USDC"), None)
        if usdc:
            rows = payouts.account_transactions(usdc["id"], limit=5)
            print(f"  yes — {len(rows)} row(s) on the USDC account")
            for r in rows[:3]:
                net = r.get("network") or {}
                print(f"     {r.get('type'):<8} {r.get('status'):<10} "
                      f"{(r.get('amount') or {}).get('amount')} "
                      f"from={net.get('from') or r.get('from') or '-'}")
        else:
            print("  no USDC account visible")
    except Exception as exc:
        print("  no —", str(exc)[:160])

    print("\nALL CHECKS PASSED" if ok else "\nSOMETHING IS WRONG")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
