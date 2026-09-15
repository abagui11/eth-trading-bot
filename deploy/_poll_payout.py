"""Poll one payout to settlement and report the elapsed time.

The processing window promised to testers has to come from a measurement.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import payouts  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--tx", required=True)
ap.add_argument("--since", type=float, default=None,
                help="unix ts the send was submitted, for true elapsed time")
args = ap.parse_args()

acct = payouts.usdc_account()
started = args.since or time.time()

while time.time() - started < 3600:
    try:
        cur = payouts.get_transaction(acct["id"], args.tx)
    except Exception as exc:
        print(f"  poll failed: {str(exc)[:120]}", flush=True)
        time.sleep(15)
        continue

    elapsed = time.time() - started
    print(
        f"  {elapsed:6.0f}s  status={cur['status']:<12} "
        f"network={cur['network_status']:<12} hash={cur['txid'] or '-'}",
        flush=True,
    )
    if cur["network_status"] in ("confirmed", "completed"):
        print(f"\nSETTLED in {elapsed / 60:.1f} minutes", flush=True)
        print(f"tx hash: {cur['txid']}", flush=True)
        raise SystemExit(0)
    if cur["status"] in ("failed", "canceled", "expired"):
        print(f"\nENDED AS {cur['status']}", flush=True)
        raise SystemExit(1)
    time.sleep(15)

print("\nstill pending after 60 minutes", flush=True)
