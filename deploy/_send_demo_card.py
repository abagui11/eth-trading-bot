"""Send one demo trade card from the server.

The same thing `/democard` does from Telegram, which is usually easier. This
exists for when you want it scripted, or want the diagnostics printed.

    python deploy/_send_demo_card.py <telegram_id>
    python deploy/_send_demo_card.py <telegram_id> --product ETH-USD --side sell

Accept is safe to press: see demo_card.py for why nothing can fill.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import demo_card    # noqa: E402
import pool         # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("telegram_id", type=int)
    ap.add_argument("--product", default="BTC-USD")
    ap.add_argument("--side", default="buy", choices=("buy", "sell"))
    ap.add_argument("--live", action="store_true",
                    help="mirror a real open position instead of a synthetic setup")
    ap.add_argument("--source", choices=("mill", "hq"),
                    help="with --live, which book to mirror from")
    ap.add_argument("--trade-id", type=int, help="mirror this trade specifically")
    args = ap.parse_args()
    uid = args.telegram_id
    mirror = args.live or args.source is not None or args.trade_id is not None

    account = pool.get_account(uid)
    cash = float(account["cash_usd"]) if account else 0.0
    print(f"telegram id : {uid}")
    print(f"cash        : ${cash:,.2f}")

    result = demo_card.send(uid, product=args.product, side=args.side,
                            mirror=mirror, source=args.source,
                            trade_id=args.trade_id)
    if not result.get("ok"):
        print(f"\nno card sent: {result.get('reason')}")
        return 1

    if result.get("mirrored_trade_id"):
        print(f"mirroring   : live {result['mirrored_source']} trade "
              f"#{result['mirrored_trade_id']}")
    print(f"{result['product']} spot : ${result['spot']:,.2f}")
    print(f"entry ${result['entry']:,.2f} · stop ${result['stop_loss']:,.2f}"
          f" · tps {result.get('take_profits')}")
    if result.get("drift_pct") is not None:
        print(f"spot is {result['drift_pct']:.1f}% off that entry")
    if result["quotes_a_size"]:
        print(f"their Accept would risk ${result['risk_usd']:,.2f} on "
              f"${result['notional_usd']:,.0f} notional")
    else:
        print("NOTE: unfunded account — the card invites them to /deposit "
              "rather than quoting a size. Honest, but probably not the "
              "render you want to film.")

    print(f"\nsent. demo ref: {result['ref']}")
    print("Accept reserves their real budget, then the watchdog releases it "
          "within ~60s with the real 'never fired' message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
