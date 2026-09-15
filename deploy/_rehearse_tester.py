"""Walk one tester end to end: register, deposit, auto-verify, withdraw, settle.

The chain reads here are **real** — real Etherscan lookups against real
transaction hashes from this deposit address — because the thing most worth
proving is that verification works on actual data rather than on fixtures we
wrote to pass. The ledger is a scratch file, so no real balance moves and the
reconciler cannot see a claim that is not backed by venue cash.

It calls the shipped sweeps rather than reimplementing their logic, with
notifications captured instead of sent, so the messages printed are exactly
what a tester and an admin would receive.

    sudo -u ethagent .venv/bin/python deploy/_rehearse_tester.py

What it cannot prove, because both need real money in motion: a brand-new
Coinbase deposit arriving, and a brand-new payout leaving. Each has been
demonstrated separately already.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config   # noqa: E402
import chain        # noqa: E402
import config       # noqa: E402
import notify       # noqa: E402
import pool         # noqa: E402

UID = -90303
ADMIN = 424242

# Real history on the deposit address: $1,000 sent from this wallet. Used as
# the deposit being proven, so the sender comparison runs against a transfer
# that genuinely happened rather than one we invented.
WALLET = "0x6549B1E2C9B3b004fca5E3C13AD8189Cf2f273B1"
DEPOSIT_TXID = "0x8cec3f784252d27f0159a111ab0b83bd4e848190ab5e2e33dbc2f98d7ac8be98"
# The other real transfer on this address ($3,000), used for the mismatch
# case: same genuine sender, deliberately registered against a different
# address, which is exactly the shape of funding from an exchange.
DEPOSIT_TXID_B = "0x8ba547524f84f0366864f217b19cd5e34721639d1aff531d23b7e2d5c0e3de45"
DEPOSIT_USD = 1000.0

# The real $2 test payout, which landed in that same wallet.
PAYOUT_USD = 2.0
PAYOUT_SUBMITTED_AT = "2026-09-15T21:20:00Z"

_ok = True
_sent: list[tuple[str, str]] = []


def check(label: str, got, want) -> None:
    global _ok
    good = abs(got - want) < 0.01 if isinstance(want, float) else got == want
    _ok = _ok and good
    print(f"  {'ok  ' if good else 'FAIL'}  {label}: {got!r}")


def show_messages(header: str) -> None:
    """Print what the tester and admin would have received, then clear."""
    if not _sent:
        print(f"  (no {header})")
        return
    for who, text in _sent:
        print(f"\n  ---- {who} " + "-" * (58 - len(who)))
        for line in text.splitlines():
            print(f"  | {line}")
    print("  " + "-" * 64)
    _sent.clear()


def main() -> int:
    if not chain.configured():
        print("ETHERSCAN_API_KEY is not set — the point of this is the real "
              "lookups, so stopping rather than skipping them.")
        return 1

    tmp = tempfile.TemporaryDirectory()
    scratch = Path(tmp.name) / "rehearsal.db"
    real_db = config.LEDGER_DB
    config.LEDGER_DB = scratch
    pool._schema_ready.clear()
    pool.init_db()
    print(f"scratch ledger  : {scratch}")
    print(f"(live ledger {real_db} is untouched)\n")

    # Capture rather than send. A throwaway id has no chat, and an admin alert
    # from a rehearsal is exactly the kind of noise that teaches people to
    # ignore the real ones.
    notify.send_pool_dm = lambda uid, text, **kw: _sent.append((f"DM to {uid}", text))
    notify.send_pool_admin_alert = lambda text, **kw: _sent.append(("ADMIN", text))

    import watchdog  # after the patch, so the sweeps see it

    try:
        print("=" * 66)
        print("1. Access and wallet registration")
        print("=" * 66)
        pool.approve_user(UID, admin_id=ADMIN)
        reg = pool.register_wallet(UID, WALLET)
        check("wallet registered", reg.get("ok"), True)
        check("starts unproven", pool.get_wallet(UID)["status"], "pending")
        check("cannot be paid yet", pool.payout_target(UID).get("reason"),
              "unverified")

        print("\n" + "=" * 66)
        print("2. Deposit claim, then auto-credit on arrival")
        print("=" * 66)
        req = pool.request_deposit(UID, DEPOSIT_USD, txid=DEPOSIT_TXID)
        check("claim filed", req.get("ok"), True)

        # Shaped the way DerivGateway.get_inbound_transfers returns them. The
        # hash is real; only the Coinbase-side id is invented, since this
        # transfer's real one belongs to a house deposit already on the books.
        events = pool.observe_chain_deposits([{
            "id": f"rehearsal-{UID}", "amount": DEPOSIT_USD,
            "txid": DEPOSIT_TXID, "currency": "USDC", "network": "ethereum",
        }])
        check("auto-credited", [e["kind"] for e in events], ["credited"])
        check("cash", float(pool.get_account(UID)["cash_usd"]), DEPOSIT_USD)

        print("\n  Arrival alone does NOT prove the wallet — money being real")
        print("  is not evidence of who sent it:")
        check("still unproven", pool.get_wallet(UID)["status"], "pending")

        print("\n" + "=" * 66)
        print("3. Wallet verification — REAL Etherscan lookup")
        print("=" * 66)
        proofs = pool.wallet_proofs_to_check()
        check("one proof queued", len(proofs), 1)
        print(f"  looking up {DEPOSIT_TXID[:22]}.. on chain...")

        watchdog._wallet_verify_sweep()
        check("wallet verified", pool.get_wallet(UID)["status"], "verified")
        target = pool.payout_target(UID)
        check("payout target", target.get("address"), WALLET.lower())
        print("\n  What the tester receives:")
        show_messages("messages")

        print("=" * 66)
        print("3b. The common failure: funded from an exchange, not a wallet")
        print("=" * 66)
        print("  A second tester registers an address they control, but the")
        print("  money arrives from somewhere else. Same real transaction,")
        print("  so the sender genuinely does not match.")
        other = -90304
        pool.approve_user(other, admin_id=ADMIN)
        pool.register_wallet(other, "0x" + "11" * 20)
        req_b = pool.request_deposit(other, DEPOSIT_USD, txid=DEPOSIT_TXID_B)
        pool.observe_chain_deposits([{
            "id": f"rehearsal-{other}", "amount": DEPOSIT_USD,
            "txid": DEPOSIT_TXID_B, "currency": "USDC", "network": "ethereum",
        }])
        check("their deposit is still credited",
              float(pool.get_account(other)["cash_usd"]), DEPOSIT_USD)
        _sent.clear()

        watchdog._wallet_verify_sweep()
        check("wallet stays unproven", pool.get_wallet(other)["status"],
              "pending")
        check("withdrawals blocked",
              pool.payout_target(other).get("reason"), "unverified")
        check("recorded once", len(pool.wallet_check_mismatches()), 1)
        print("\n  What both sides receive:")
        show_messages("messages")

        print("  Re-running must not re-alert — an admin who gets this every")
        print("  minute stops reading them:")
        watchdog._wallet_verify_sweep()
        check("silent on the second pass", len(_sent), 0)
        _sent.clear()

        print("\n" + "=" * 66)
        print("4. Withdrawal request — caps, holds and refunds")
        print("=" * 66)
        reserve = float(bot_config.POOL_WITHDRAWAL_FEE_RESERVE_USD)
        check("below the minimum is refused",
              pool.request_withdrawal(UID, 5.0).get("reason"),
              "below_minimum")
        # The per-request cap is checked before the balance, so a huge number
        # is refused as `above_max` rather than as insufficient funds. Both
        # limits are tested, since they refuse for different reasons and a
        # tester deserves the one that tells them something useful.
        check("above the per-request cap",
              pool.request_withdrawal(UID, 99_000.0).get("reason"),
              "above_max")
        check("within the cap but more than they have",
              pool.request_withdrawal(UID, DEPOSIT_USD + 500.0).get("reason"),
              "insufficient_available")

        w = pool.request_withdrawal(UID, 100.0)
        check("queued", w.get("ok"), True)
        wid = int(w["withdrawal_id"])
        check("held at request time",
              float(pool.get_account(UID)["cash_usd"]),
              DEPOSIT_USD - 100.0 - reserve)
        check("frozen destination",
              pool.get_withdrawal(wid)["to_address"], WALLET.lower())

        print("\n  A rejection returns every cent:")
        pool.decide_withdrawal(wid, admin_id=ADMIN, approve=False)
        check("refunded in full", float(pool.get_account(UID)["cash_usd"]),
              DEPOSIT_USD)
        _sent.clear()

        print("\n" + "=" * 66)
        print("5. Settlement confirmation — REAL chain lookup")
        print("=" * 66)
        print("  Driving a payout to 'submitted' and letting the sweep find")
        print("  the actual $2 that landed in this wallet earlier today.")

        # The real payout was $2, below the $50 floor, so the floor is lifted
        # for this step only — the point here is the settlement read, and the
        # floor was already exercised above.
        original_min = bot_config.POOL_MIN_WITHDRAWAL_USD
        bot_config.POOL_MIN_WITHDRAWAL_USD = 1.0
        w2 = pool.request_withdrawal(UID, PAYOUT_USD)
        bot_config.POOL_MIN_WITHDRAWAL_USD = original_min
        wid2 = int(w2["withdrawal_id"])
        pool.decide_withdrawal(wid2, admin_id=ADMIN, approve=True)
        pool.mark_withdrawal_submitting(wid2)
        pool.mark_withdrawal_submitted(wid2, cb_tx_id="rehearsal-cb",
                                       fee_usd=0.148356)
        # Backdate to just before the real send, so the time floor admits it.
        conn = sqlite3.connect(scratch)
        with conn:
            conn.execute("UPDATE pool_withdrawals SET submitted_at = ? WHERE id = ?",
                         (PAYOUT_SUBMITTED_AT, wid2))
        conn.close()
        check("submitted", pool.get_withdrawal(wid2)["status"], "submitted")
        _sent.clear()

        watchdog._settle_sweep()
        row = pool.get_withdrawal(wid2)
        check("settled from the chain", row["status"], "settled")
        check("real tx recorded", str(row["txid"] or "")[:10], "0x29444be7")
        print("\n  What the tester receives:")
        show_messages("messages")

        print("=" * 66)
        print("6. Books")
        print("=" * 66)
        account = pool.get_account(UID)
        spent = round(PAYOUT_USD + 0.148356, 2)
        check("cash", float(account["cash_usd"]), DEPOSIT_USD - spent)
        print(f"  deposited ${DEPOSIT_USD:,.2f}, withdrew ${PAYOUT_USD:,.2f} "
              f"plus ${0.148356:.6f} network fee")
        print(f"  every movement is reconstructable from pool_events")
    finally:
        config.LEDGER_DB = real_db
        pool._schema_ready.clear()
        tmp.cleanup()

    print("\n" + ("ALL CHECKS PASSED" if _ok else "SOMETHING FAILED"))
    print(f"live ledger untouched; tester cash there: "
          f"${pool.total_tester_cash():,.2f}")
    return 0 if _ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
