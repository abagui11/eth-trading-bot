"""Pre-recording checklist: is everything the demo depends on actually ready?

Run this before filming. Each line is something that, if wrong, produces a
take you have to throw away -- an unset deposit address, a missing Etherscan
key so the wallet never verifies, payouts halted so the withdrawal sits in the
queue.

    python deploy/_demo_preflight.py [tester_telegram_id]
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config   # noqa: E402
import config       # noqa: E402
import pool         # noqa: E402

_ok = True


def check(label: str, good: bool, detail: str = "") -> None:
    global _ok
    _ok = _ok and good
    print(f"  {'ok  ' if good else 'FAIL'}  {label}{': ' + detail if detail else ''}")


def main() -> int:
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else None

    print("Pool configuration")
    check("pool enabled", bool(bot_config.POOL_ENABLED))
    check("deposit address set", bool(config.POOL_DEPOSIT_ADDRESS),
          str(config.POOL_DEPOSIT_ADDRESS))
    check("deposit network named", bool(config.POOL_DEPOSIT_CHAIN),
          str(config.POOL_DEPOSIT_CHAIN))
    admins = pool.admin_ids()
    check("at least one admin", bool(admins), str(admins))

    print("\nWithdrawal path")
    check("payouts enabled", bool(bot_config.POOL_PAYOUTS_ENABLED))
    check("payouts not halted", not pool.payouts_halted(),
          str(pool.payouts_halted() or "running"))
    check("transfer key installed", bool(config.COINBASE_TRANSFER_KEY_NAME))
    try:
        import chain
        check("etherscan key installed", chain.configured())
        if chain.configured() and config.POOL_DEPOSIT_ADDRESS:
            seen = chain.inbound_usdc(config.POOL_DEPOSIT_ADDRESS, limit=5)
            check("chain reads working", bool(seen),
                  f"{len(seen)} inbound transfers visible")
    except Exception as exc:
        check("chain module", False, str(exc)[:80])

    print("\nBooks")
    try:
        snap = pool.reconcile_snapshot()
        check("reconcile healthy", bool(snap.get("ok")),
              f"venue ${float(snap.get('venue_assets_usd') or 0):,.2f} vs "
              f"claims ${float(snap.get('tester_cash_usd') or 0):,.2f}")
    except Exception as exc:
        print(f"  --    reconcile snapshot unavailable: {str(exc)[:80]}")
    check("intents not frozen", not pool.is_frozen(),
          str(pool.is_frozen() or "open"))

    print("\nLimits the demo will hit")
    print(f"  min deposit    ${float(bot_config.POOL_MIN_DEPOSIT_USD):,.0f}")
    print(f"  min withdrawal ${float(bot_config.POOL_MIN_WITHDRAWAL_USD):,.0f}")
    print(f"  min equity to Accept "
          f"${float(bot_config.POOL_MIN_EQUITY_USD):,.0f}")
    print(f"  risk per Accept {float(bot_config.POOL_RISK_PCT) * 100:.1f}% "
          "of available cash")

    if uid is not None:
        print(f"\nTester {uid}")
        account = pool.get_account(uid)
        wallet = pool.get_wallet(uid)
        print(f"  approved : {pool.is_approved(uid)}")
        print(f"  cash     : ${float(account['cash_usd']) if account else 0:,.2f}")
        print(f"  wallet   : "
              f"{wallet['address'] + ' (' + wallet['status'] + ')' if wallet else 'none'}")
        target = pool.payout_target(uid)
        print(f"  can be paid : {target.get('ok')} "
              f"({target.get('reason') or target.get('address')})")
        if account and float(account["cash_usd"]) >= float(
            bot_config.POOL_MIN_EQUITY_USD
        ):
            print("  a demo card would quote a real size for this account")
        else:
            print(f"  NOTE: below ${float(bot_config.POOL_MIN_EQUITY_USD):,.0f} "
                  "equity, so a card invites them to /deposit instead of "
                  "quoting a size")

    print("\n" + ("READY" if _ok else "NOT READY — fix the FAILs above"))
    return 0 if _ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
