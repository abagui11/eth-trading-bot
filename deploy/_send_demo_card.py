"""Send one demo trade card to one Telegram id, on demand.

HQ runs every 30 minutes and most cycles legitimately find no trade, so a real
card cannot be summoned for a recording. This sends a card that is real in
every way that matters -- the live renderer, the live sizing rule, the live
reservation on Accept -- while being structurally unable to place an order.

    python deploy/_send_demo_card.py <telegram_id>
    python deploy/_send_demo_card.py <telegram_id> --product ETH-USD --side buy

Why Accept is safe, and why it is safe by construction rather than by care:
every executor resolves pooled intents **by ref**, either a live pending cycle
id or `mill_<id>`. The ref here is `demo_<token>`, which matches neither, so
there is no code path that could turn it into a position. The watchdog's
stale-intent sweep then finds a ref that is not active, returns the reserve,
and sends the genuine "that order never fired" message about a minute later --
which is itself worth having on camera.

The card is labelled. A demo that hides being a demo is how a screenshot ends
up quoted back as a real trade.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config     # noqa: E402
import display_summary  # noqa: E402
import notify         # noqa: E402
import pool           # noqa: E402
import research       # noqa: E402
import telegram_ui    # noqa: E402
from models import Suggestion  # noqa: E402

BANNER = (
    "DEMO CARD — illustration only. Accept reserves your budget exactly as a "
    "real card would, then releases it, because there is no order behind this "
    "one."
)


def build(product: str, side: str, spot: float) -> Suggestion:
    """A plausible setup around the live price, so the numbers look real.

    Levels are derived from the current spot rather than hardcoded: a card
    quoting a price from last week is the first thing a viewer notices.
    """
    buy = side == "buy"
    # ~0.9% to the stop and a 2.4R first target: an ordinary shape for this
    # book, not a flattering one.
    stop_dist = spot * 0.009
    entry = spot * (0.999 if buy else 1.001)
    stop = entry - stop_dist if buy else entry + stop_dist
    tps = [
        entry + stop_dist * mult if buy else entry - stop_dist * mult
        for mult in (2.4, 3.6, 5.0)
    ]
    return Suggestion(
        action="spot_buy" if buy else "spot_sell",
        size=0.0,
        entry=round(entry, 2),
        stop_loss=round(stop, 2),
        take_profits=[round(t, 2) for t in tps],
        risk_reward=2.4,
        rationale=(
            "DEMO — not a live trade idea.\n\n"
            "Higher-timeframe structure aligns with an M5 order-block entry "
            "at the fib discount, with the stop beyond the block's origin.\n\n"
            "Market context:\n"
            f"• {'bullish' if buy else 'bearish'} HTF structure\n"
            "• M5 order block unmitigated\n"
            "• first target at prior session high/low"
        ),
        product_id=product,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("telegram_id", type=int)
    ap.add_argument("--product", default="BTC-USD")
    ap.add_argument("--side", default="buy", choices=("buy", "sell"))
    args = ap.parse_args()
    uid = args.telegram_id

    if not bot_config.POOL_ENABLED:
        print("POOL_ENABLED is off — the card would render the demo-book "
              "sizing lines instead of pool sizing.")
        return 1
    if not pool.is_approved(uid):
        print(f"{uid} is not approved, so the card would say 'Access "
              "required' instead of showing a size. Admit them first.")
        return 1

    account = pool.get_account(uid)
    cash = float(account["cash_usd"]) if account else 0.0
    print(f"telegram id : {uid}")
    print(f"cash        : ${cash:,.2f}")
    if cash <= 0:
        print("\nNOTE: with no cash the card will invite them to /deposit "
              "rather than quote a size. That is the honest render, but it is "
              "probably not the one you want to film.")

    spots = research.get_spot_prices()
    spot = float(spots.get(args.product) or 0)
    if spot <= 0:
        print(f"could not read a spot price for {args.product}")
        return 1
    print(f"{args.product} spot : ${spot:,.2f}")

    suggestion = build(args.product, args.side, spot)
    token = uuid.uuid4().hex[:12]

    # The real renderer, with the tester's id, so the per-user size lines are
    # produced by the same code a live card uses.
    body = display_summary.build_card_body(suggestion, telegram_id=uid)
    text = f"{BANNER}\n\n{body}"

    prosp = pool.prospective_accept(
        uid, entry=float(suggestion.entry), stop_loss=float(suggestion.stop_loss)
    )
    if prosp.get("ok"):
        print(f"their Accept would risk ${float(prosp['risk_usd']):,.2f} "
              f"on ${float(prosp['notional_usd']):,.0f} notional")

    sent = notify.send_pool_dm_with_keyboard(
        uid, text[:4096], telegram_ui.pool_demo_keyboard(token)
    )
    if not sent:
        print("\nsend failed — check the bot can DM this id (they must have "
              "messaged it at least once).")
        return 1

    print(f"\nsent. demo ref: demo_{token}")
    print("Accept reserves their real budget, then the watchdog releases it "
          "within ~60s with the real 'never fired' message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
