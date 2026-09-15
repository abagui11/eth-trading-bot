"""How long the real $2 payout actually took, end to end.

Onboarding copy has to promise a processing time, and until now the only
number we had was "the Coinbase hold cleared sometime before I next looked",
which is an upper bound on my polling interval rather than on the payout. The
chain has the arrival timestamp, so this measures it properly.

    python deploy/_payout_latency.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chain          # noqa: E402

DEST = "0x6549B1E2C9B3b004fca5E3C13AD8189Cf2f273B1"
TXID = "0x29444be7c50e346dfe702c9c189241805931e92dfb5f0e62a43ddded10cdcebb"


def main() -> int:
    transfer = None
    for t in chain.usdc_transfers(DEST, limit=25):
        if t["txid"] == TXID:
            transfer = t
            break
    if transfer is None:
        print("payout transfer not found")
        return 1

    landed = datetime.fromtimestamp(transfer["timestamp"], tz=timezone.utc)
    now = datetime.now(timezone.utc)

    print(f"landed on-chain : {landed:%Y-%m-%d %H:%M:%S} UTC  "
          f"(epoch {transfer['timestamp']})")
    print(f"now             : {now:%Y-%m-%d %H:%M:%S} UTC")
    print(f"confirmations   : {transfer['confirmations']:,}")
    print(f"amount          : ${transfer['amount_usd']:,.6f}")
    print()
    print("Compare against when payouts.send returned. Whatever the gap is,")
    print("it is the number onboarding should quote — and it is measured at")
    print("the tester's wallet, not at the venue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
