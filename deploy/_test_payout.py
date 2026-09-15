"""Send one small REAL withdrawal, and time it end to end.

This is the only way to learn three things we are otherwise guessing at:
whether the send endpoint works at all with this key, whether Coinbase's
address allowlist is on (it refuses non-allowlisted destinations), and how
long a payout actually takes -- which is the number the onboarding copy has to
promise testers. Guessing that number and being wrong is worse than not
promising one.

Dry run by default. Nothing moves without --yes.

    sudo -u ethagent .venv/bin/python deploy/_test_payout.py \
        --to 0x... --amount 2 [--yes]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import payouts  # noqa: E402

POLL_SEC = 15
MAX_WAIT_SEC = 1800


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="destination address")
    ap.add_argument("--amount", type=float, required=True, help="USDC")
    ap.add_argument("--network", default="ethereum")
    ap.add_argument("--yes", action="store_true", help="actually send")
    args = ap.parse_args()

    acct = payouts.usdc_account()
    print(f"source account : {acct['id']}")
    print(f"balance        : ${acct['balance']:,.2f} USDC")
    print(f"destination    : {args.to}")
    print(f"amount         : ${args.amount:,.2f} USDC over {args.network}")

    if args.amount > acct["balance"]:
        print("\nrefusing: amount exceeds the spot USDC balance")
        return 2

    if not args.yes:
        print("\nDRY RUN — nothing sent. Re-run with --yes to move funds.")
        return 0

    # A stable idem key, so re-running this script cannot double-send. Tied to
    # the destination and amount rather than the clock for the same reason.
    idem = f"testpayout:{args.to[-8:]}:{args.amount:.2f}"
    print(f"\nidem           : {idem}")

    started = time.time()
    try:
        sent = payouts.send(
            account_id=acct["id"], to_address=args.to,
            amount_usd=args.amount, idem=idem, network=args.network,
            description="EVA payout path test",
        )
    except payouts.PayoutError as exc:
        print(f"\nSEND FAILED: {exc}")
        if exc.submitted:
            print("!! The request reached Coinbase and the outcome is UNKNOWN.")
            print("!! Check the account before retrying — do not resend blindly.")
        return 1

    print(f"submitted in   : {time.time() - started:.1f}s")
    print(f"coinbase tx    : {sent['id']}")
    print(f"status         : {sent['status']} / network {sent['network_status']}")

    print("\npolling until it settles on-chain...")
    while time.time() - started < MAX_WAIT_SEC:
        time.sleep(POLL_SEC)
        try:
            cur = payouts.get_transaction(acct["id"], sent["id"])
        except payouts.PayoutError as exc:
            print(f"  poll failed: {str(exc)[:100]}")
            continue
        elapsed = time.time() - started
        print(f"  {elapsed:6.0f}s  status={cur['status']:<12} "
              f"network={cur['network_status']:<12} hash={cur['txid'] or '-'}")
        if cur["network_status"] in ("confirmed", "completed"):
            print(f"\nSETTLED in {elapsed / 60:.1f} minutes")
            print(f"tx hash: {cur['txid']}")
            print("\n-> Use this to set the processing window promised at onboarding.")
            return 0
        if cur["status"] in ("failed", "canceled", "expired"):
            print(f"\nENDED AS {cur['status']}")
            return 1

    print(f"\nstill pending after {MAX_WAIT_SEC / 60:.0f} minutes — check manually")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
