"""Prove the chain reads against transfers we already know the truth about.

Unit tests pin the parsing against fixtures we wrote, which cannot catch the
one error that would matter most: a wrong USDC contract address, or Etherscan
returning a shape we guessed at. The deposit address already has real
transfers on it, so this checks the code against them.

    python deploy/_verify_chain.py

Read-only. Prints what it sees and never writes to the ledger.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chain          # noqa: E402
import config         # noqa: E402
import pool           # noqa: E402


def main() -> int:
    if not chain.configured():
        print("ETHERSCAN_API_KEY is not set. Add it to .env:")
        print("  ETHERSCAN_API_KEY=...")
        return 1

    address = config.POOL_DEPOSIT_ADDRESS
    if not address:
        print("POOL_DEPOSIT_ADDRESS is not set.")
        return 1

    print(f"deposit address : {address}")
    print(f"usdc contract   : {chain.USDC_CONTRACT}")
    print()

    try:
        transfers = chain.inbound_usdc(address, limit=25)
    except chain.ChainError as exc:
        print(f"FAILED: {exc}")
        print("\nIf this says NOTOK/invalid key, the key is not active yet —")
        print("Etherscan keys take a minute, and v2 needs the key to be on a")
        print("plan that includes mainnet.")
        return 1

    if not transfers:
        print("No inbound USDC found. That is the thing to look at: this")
        print("address HAS received USDC, so an empty answer means the")
        print("contract address or the endpoint is wrong, not that it is")
        print("quiet.")
        return 1

    print(f"{len(transfers)} inbound USDC transfer(s), newest first:\n")
    for t in transfers:
        print(f"  ${t['amount_usd']:>12,.2f}  from {t['from']}")
        print(f"  {'':>14}  tx {t['txid']}")
        print(f"  {'':>14}  {t['confirmations']:,} confirmations\n")

    # The sender is the whole point: this is what Coinbase would not tell us.
    senders = {t["from"] for t in transfers}
    print(f"distinct senders: {len(senders)}")
    print("This is the field Coinbase omits, and the reason wallets could")
    print("never reach 'verified' before now.\n")

    # Cross-check against what the ledger believes, without changing it.
    pending = pool.wallet_proofs_to_check()
    if not pending:
        print("No wallets are currently awaiting proof.")
        return 0

    print(f"{len(pending)} wallet(s) awaiting proof — dry run:\n")
    for proof in pending:
        result = chain.verify_deposit(proof["txid"], to_address=address)
        if not result.get("ok"):
            print(f"  {proof['telegram_id']}: {result.get('reason')} "
                  f"{result.get('detail', '')}")
            continue
        sender = str(result["sender"])
        verdict = "MATCH" if sender == proof["address"] else "MISMATCH"
        print(f"  {proof['telegram_id']}: {verdict}")
        print(f"      registered {proof['address']}")
        print(f"      actual     {sender}")
    print("\nNothing was written. The watchdog applies these on its next pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
