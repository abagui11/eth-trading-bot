"""Walk a tester through a real card and print every message they'd see.

Runs against a **scratch copy** of `ledger.db`, so reserves and stakes written
here are thrown away and the real book is never touched. The fill itself is
stubbed per scenario rather than executed -- the point is to read the copy and
the bookkeeping on each outcome, not to re-test the executor.

    python deploy/_walk_tester_card.py 8708390551
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

_SCRATCH = os.path.join(tempfile.gettempdir(), "_walk_ledger.db")
shutil.copy(config.LEDGER_DB, _SCRATCH)
config.LEDGER_DB = _SCRATCH

import bot  # noqa: E402
import bot_config  # noqa: E402
import demo_card  # noqa: E402
import pool  # noqa: E402
import research  # noqa: E402
import telegram_ui  # noqa: E402
import trade_ideas_bridge as bridge  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def quote(text: str) -> None:
    for line in str(text).splitlines():
        print(f"  | {line}")


def state(uid: int) -> str:
    acct = pool.get_account(uid) or {}
    return (f"cash ${float(acct.get('cash_usd') or 0):,.2f}  "
            f"reserved ${float(acct.get('reserved_usd') or 0):,.2f}")


def pick_idea() -> int | None:
    """Newest mill idea whose pool window is still open."""
    conn = bridge._connect()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT id FROM ideas ORDER BY id DESC LIMIT 40"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        if bridge.idea_pool_open(int(row[0])):
            return int(row[0])
    return int(rows[0][0]) if rows else None


def accept(uid: int, idea_id: int, verdict: dict | None, *, label: str) -> None:
    """Drive the real Accept handler with the fill stubbed to one outcome."""
    print(f"\n--- {label} ---")
    before = state(uid)
    if verdict is None:
        reply = bot._pool_mill_accept(idea_id, uid)
    else:
        with patch.object(bridge, "request_manual_fill", return_value=verdict), \
                patch.object(bridge, "may_fill", return_value=True):
            reply = bot._pool_mill_accept(idea_id, uid)
    print(f"  ledger before: {before}")
    print(f"  ledger after:  {state(uid)}")
    print("  they see:")
    quote(reply)
    # Reset so each scenario starts from the same place.
    pool.release_intents(f"mill_{idea_id}", status="missed")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    uid = int(sys.argv[1])

    rule(f"account {uid}")
    print(f"  POOL_ENABLED     {bot_config.POOL_ENABLED}")
    print(f"  approved         {pool.is_approved(uid)}")
    print(f"  funded           {pool.is_funded(uid)}")
    print(f"  {state(uid)}")
    print(f"  withdrawable     ${pool.withdrawable_usd(uid):,.2f}")
    print(f"  may trigger fill {bridge.may_fill(uid)}"
          f"   (LIVE_MILL_ANY_ACCEPT_FILLS={bot_config.LIVE_MILL_ANY_ACCEPT_FILLS})")
    print(f"  scratch ledger   {config.LEDGER_DB}")

    rule("the card, as they receive it")
    trade = demo_card.pick_trade()
    if trade is not None:
        sug = demo_card.build_from_trade(trade)
        print(f"  mirroring open trade #{trade.get('id')} "
              f"({trade.get('product_id')} {trade.get('side')})")
    else:
        spot = float(research.get_spot_prices().get("BTC-USD") or 0)
        sug = demo_card.build_suggestion("BTC-USD", "buy", spot)
        print(f"  no open position to mirror -- synthetic BTC long "
              f"off spot ${spot:,.2f}")
    sizing = pool.prospective_accept(
        uid, entry=float(sug.entry), stop_loss=float(sug.stop_loss)
    )
    print(f"  entry ${float(sug.entry):,.2f}  stop ${float(sug.stop_loss):,.2f}")
    print(f"  their size line: ${float(sizing.get('notional_usd') or 0):,.2f} "
          f"notional, ${float(sizing.get('risk_usd') or 0):,.2f} at risk")

    idea_id = pick_idea()
    if idea_id is None:
        print("\nno mill ideas in the hub -- cannot walk Accept")
        return 1
    rule(f"Accept on mill idea #{idea_id}  (window open: "
         f"{bridge.idea_pool_open(idea_id)})")

    fill = 3_500.0
    accept(uid, idea_id, {
        "executed": True,
        "result": {"trade_id": -1, "fill": fill},
    }, label="it fills (no stake row on scratch -> falls back to reserve copy)")

    for reason, label in (
        ("chase", "refused: price drifted past the entry"),
        ("rr_floor", "refused: never qualified at mint"),
        ("loss_cooldown", "refused: loss cooldown on that product/side"),
        ("exposure", "refused: sleeve already full"),
    ):
        accept(uid, idea_id, {"executed": False, "skip_reason": reason},
               label=label)

    rule("what they no longer see")
    for data in (telegram_ui.CB_OPEN, f"{telegram_ui.CB_OPEN_SIZE_PREFIX}2500",
                 f"{telegram_ui.CB_TRADE_JOIN_PREFIX}x", telegram_ui.CB_MY_BOOK):
        print(f"  {data:<24} refused: {bot._is_legacy_paper(data)}")
    labels = [b.text for row in telegram_ui.main_keyboard().inline_keyboard
              for b in row]
    print(f"  menu: {labels}")

    rule("safety backstop")
    print(f"  POOL_INTENT_TTL_MIN = {bot_config.POOL_INTENT_TTL_MIN} min")
    print("  An Accept that somehow stays pending is force-released by "
          "expire_stale_intents\n  after that, with the money-back DM sent.")

    print(f"\nreal ledger untouched: {os.path.basename(_SCRATCH)} was a copy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
