"""Read-only: what the new reconciler would decide, right now.

Calls get_cash_assets() against the live account and runs the same comparison
the watchdog does, WITHOUT writing a snapshot or touching the freeze flag, so
the fix can be checked against real balances before it goes on the cadence.

    sudo -u ethagent .venv/bin/python deploy/_verify_reconcile.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config  # noqa: E402
import pool  # noqa: E402
from coinbase_deriv import get_gateway  # noqa: E402


def main() -> int:
    assets = get_gateway().get_cash_assets()
    claims = pool.total_tester_cash()
    tol = float(bot_config.POOL_RECON_TOLERANCE_USD)

    print("assets seen by the NEW reconciler")
    for cur, amt in sorted(assets["wallets"].items()):
        print(f"    spot {cur:<5} ${amt:>12,.2f}")
    print(f"    futures     ${assets['futures_usd']:>12,.2f}  (cfm_usd_balance)")
    print(f"    TOTAL       ${assets['total_usd']:>12,.2f}")
    print(f"    truncated   {assets['truncated']}")

    old = float(get_gateway().get_account_summary().get("equity") or 0.0)
    print(f"\nwhat the OLD reconciler read: ${old:,.2f} (total_usd_balance)")

    print(f"\ntester claims:        ${claims:,.2f}")
    print(f"tolerance:            ${tol:,.2f}")
    headroom = assets["total_usd"] - claims
    print(f"headroom (new):       ${headroom:,.2f}  -> ok={headroom >= -tol}")
    print(f"headroom (old):       ${old - claims:,.2f}  -> ok={old - claims >= -tol}")

    print(f"\ncurrently frozen: {pool.intents_frozen()}")

    # The scenario that motivated the fix.
    print("\nif a tester deposited $1,000 today:")
    for label, base in (("new", assets["total_usd"]), ("old", old)):
        h = base - (claims + 1000.0)
        verdict = "OK" if h >= -tol else "FREEZE + page ops"
        print(f"    {label}: headroom ${h:>10,.2f}  -> {verdict}")

    print(f"\ntradeable (futures collateral): ${assets['tradeable_usd']:,.2f}")
    if assets["tradeable_usd"] < claims + 1000.0:
        print("    NOTE: covered but not deployable — a spot->futures transfer is")
        print("    needed before that cash can margin a position. Not a shortfall.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
