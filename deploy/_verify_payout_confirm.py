"""Check payout confirmation against the one real payout we already know about.

The $2 test send landed in 0x6549…73B1. That gives us ground truth for the
half of the chain reads that unit tests can only fake: if `confirm_payout`
cannot find a payout we watched arrive, it would never confirm a tester's
either, and withdrawals would sit in `submitted` forever.

    python deploy/_verify_payout_confirm.py

Read-only.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chain          # noqa: E402

DEST = "0x6549B1E2C9B3b004fca5E3C13AD8189Cf2f273B1"
AMOUNT = 2.0
# Just before the test send went out (2026-09-15 21:20 UTC).
SUBMITTED = 1789500000


def main() -> int:
    if not chain.configured():
        print("ETHERSCAN_API_KEY is not set.")
        return 1

    print(f"destination : {DEST}")
    print(f"looking for : ${AMOUNT:,.2f} after {SUBMITTED}\n")

    found = chain.confirm_payout(DEST, AMOUNT, after_timestamp=SUBMITTED)
    if found.get("ok"):
        print("CONFIRMED")
        print(f"  tx            {found['txid']}")
        print(f"  amount        ${found['amount_usd']:,.6f}")
        print(f"  confirmations {found['confirmations']:,}")
        print(f"  timestamp     {found['timestamp']}")
    else:
        print(f"NOT CONFIRMED: {found.get('reason')} "
              f"{found.get('detail', '')}")

    # The time floor is what stops an older transfer of the same size reading
    # as this payout settling. Prove it actually bites.
    stale = chain.confirm_payout(DEST, AMOUNT, after_timestamp=4_000_000_000)
    print(f"\nwith an impossible time floor: {stale.get('reason')} "
          f"(expected not_seen_yet — the floor is doing its job)")

    print("\nrecent USDC at this address:")
    for t in chain.usdc_transfers(DEST, limit=8):
        way = "in " if t["to"] == DEST.lower() else "out"
        print(f"  {way} ${t['amount_usd']:>12,.6f}  {t['timestamp']}  "
              f"{t['txid'][:20]}..")
    return 0 if found.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
