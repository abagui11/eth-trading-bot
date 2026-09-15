"""Find the send body Coinbase accepts. STOPS at the first success.

A valid UUID idem is still rejected, so "Param: Idem" is likely just the first
field the validator names rather than the actual fault. These variants drop
one suspect at a time.

Every attempt is a REAL send, so the loop breaks the instant one works -- the
failure mode to avoid here is discovering the right shape three times over.
Dry run unless --yes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import payouts  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--to", required=True)
ap.add_argument("--amount", type=float, default=2.0)
ap.add_argument("--yes", action="store_true")
args = ap.parse_args()

acct = payouts.usdc_account()
amt = f"{args.amount:.2f}"
idem = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"probe:{args.to}:{amt}"))

variants: list[tuple[str, dict]] = [
    ("full: type/to/amount/currency/network/idem/description", {
        "type": "send", "to": args.to, "amount": amt, "currency": "USDC",
        "network": "ethereum", "idem": idem, "description": "probe"}),
    ("no description", {
        "type": "send", "to": args.to, "amount": amt, "currency": "USDC",
        "network": "ethereum", "idem": idem}),
    ("no network", {
        "type": "send", "to": args.to, "amount": amt, "currency": "USDC",
        "idem": idem}),
    ("no idem", {
        "type": "send", "to": args.to, "amount": amt, "currency": "USDC",
        "network": "ethereum"}),
    ("minimal: type/to/amount/currency", {
        "type": "send", "to": args.to, "amount": amt, "currency": "USDC"}),
    ("idem without dashes", {
        "type": "send", "to": args.to, "amount": amt, "currency": "USDC",
        "idem": idem.replace("-", "")}),
    ("network=ethereum-mainnet", {
        "type": "send", "to": args.to, "amount": amt, "currency": "USDC",
        "network": "ethereum-mainnet", "idem": idem}),
]

print(f"account {acct['id']}  balance ${acct['balance']:,.2f}")
print(f"destination {args.to}  amount {amt}\n")

if not args.yes:
    for label, body in variants:
        print(f"-- {label}\n   {json.dumps(body)}")
    print("\nDRY RUN — nothing sent.")
    raise SystemExit(0)

for label, body in variants:
    try:
        res = payouts._request(
            "POST", f"/api/v2/accounts/{acct['id']}/transactions", body=body
        )
        print(f"  OK   {label}")
        print("  ACCEPTED BODY:", json.dumps(body))
        print("  response:", json.dumps(res)[:400])
        print("\nstopping — money has moved once.")
        break
    except payouts.PayoutError as exc:
        msg = str(exc)
        detail = msg.split("HTTP", 1)[-1][:160]
        print(f"  fail {label}: {detail}")
else:
    print("\nno variant accepted.")
