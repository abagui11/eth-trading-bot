"""One demo trade card, on demand.

HQ runs on a cycle and most cycles legitimately find no trade, so the most
important thing in the product -- a card arriving with your own size on it,
and Accept -- cannot be summoned for a demo or a walkthrough. This builds one
that is real in every way that matters and cannot trade.

Two sources, and the choice matters:

* ``mirror`` copies the levels of a **real open trade** off ``live_trades``,
  including its original rationale. Nothing about the setup is invented, so
  the size it quotes is the size that account would really have taken on that
  trade -- which is the only way to see what a real entry looks like at a
  given portfolio size.
* the synthetic setup builds plausible levels around current spot. Use it when
  nothing is open.

The card is rendered by the **live** builder with the **live** sizing rule, so
the size it quotes is the size a real card would quote for that account. A
demo showing numbers the live path would not produce is a demo of something
that does not exist.

Accept cannot become a position, and that is structural rather than careful:
every executor resolves pooled intents *by ref* (``pool.pending_intents(ref)``
inside ``extra_contracts_for`` and ``open_stakes``), and the ref here is
``demo_<token>`` -- matching no live pending cycle id and no ``mill_<id>``.
There is no code path from a demo ref to an order. The watchdog's stale-intent
sweep then returns the reserve with the genuine "that order never fired"
message, which is how every non-filling Accept behaves.

Shared by ``/democard`` and ``deploy/_send_demo_card.py`` so there is one
implementation rather than two that drift.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import bot_config
from models import Suggestion

logger = logging.getLogger(__name__)

REF_PREFIX = "demo_"

BANNER = (
    "DEMO CARD — illustration only. Accept reserves your budget exactly as a "
    "real card would, then releases it, because there is no order behind this "
    "one."
)

MIRROR_BANNER = (
    "DEMO CARD — this mirrors a real open position ({label}). The levels and "
    "the size below are real; the card is not an offer, and Accept reserves "
    "your budget and then returns it."
)

PRODUCTS = {
    "btc": "BTC-USD", "bitcoin": "BTC-USD", "btc-usd": "BTC-USD",
    "eth": "ETH-USD", "ether": "ETH-USD", "eth-usd": "ETH-USD",
}
SIDES = {
    "buy": "buy", "long": "buy", "b": "buy",
    "sell": "sell", "short": "sell", "s": "sell",
}
# 'mill'/'hq' both pick a real trade and narrow which book it comes from.
MIRRORS = {"live": None, "real": None, "open": None, "mill": "mill", "hq": "hq"}


def parse_args(args: list[str], *, default_id: int) -> dict[str, Any]:
    """Read `/democard [id] [live|mill|hq] [product] [side] [#trade]` in any order.

    Order-insensitive on purpose: this gets typed mid-recording, and having to
    remember positions is exactly when it gets typed wrong. Telegram ids are
    long, trade ids are short, which is what separates `85` from `8708390551`.
    """
    out: dict[str, Any] = {
        "telegram_id": default_id, "product": "BTC-USD", "side": "buy",
        "mirror": False, "source": None, "trade_id": None,
    }
    for raw in args:
        token = str(raw).strip().lower().lstrip("-#")
        if token in MIRRORS:
            out["mirror"] = True
            out["source"] = MIRRORS[token] or out["source"]
        elif token in PRODUCTS:
            out["product"] = PRODUCTS[token]
        elif token in SIDES:
            out["side"] = SIDES[token]
        elif token.isdigit():
            if len(token) >= 5:
                out["telegram_id"] = int(token)
            else:
                out["trade_id"] = int(token)
                out["mirror"] = True
    return out


def build_suggestion(product: str, side: str, spot: float) -> Suggestion:
    """A plausible setup around the live price.

    Levels come off the current spot rather than being hardcoded: a card
    quoting last week's price is the first thing a viewer notices. The shape
    (~0.9% stop, 2.4R first target) is an ordinary one for this book rather
    than a flattering one.
    """
    buy = side == "buy"
    stop_dist = spot * 0.009
    entry = spot * (0.999 if buy else 1.001)
    stop = entry - stop_dist if buy else entry + stop_dist
    tps = [
        entry + stop_dist * m if buy else entry - stop_dist * m
        for m in (2.4, 3.6, 5.0)
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


def _original_rationale(trade: dict[str, Any]) -> str | None:
    """The thesis the trade was actually taken on, if it can still be found.

    HQ writes a full rationale to `suggestions`; mill keeps a one-line title
    in the ideas hub. Either is better than describing the setup generically,
    because the point of mirroring is that nothing is invented.
    """
    cycle_id = str(trade.get("cycle_id") or "")

    if cycle_id.startswith("mill_"):
        try:
            import trade_ideas_bridge
            idea = trade_ideas_bridge.get_idea(int(cycle_id.split("_", 1)[1]))
            if idea and idea.get("title"):
                return str(idea["title"])
        except Exception:
            logger.debug("demo card: mill idea lookup failed", exc_info=True)

    try:
        import ledger
        row = ledger.get_suggestion_by_cycle_id(cycle_id)
        if row and row.get("rationale"):
            return str(row["rationale"])
    except Exception:
        logger.debug("demo card: suggestion lookup failed", exc_info=True)
    return None


def pick_trade(trade_id: int | None = None,
               source: str | None = None) -> dict[str, Any] | None:
    """The trade to mirror: the one asked for, else the newest still open."""
    import live_ledger

    if trade_id is not None:
        trade = live_ledger.get_trade(trade_id)
        return trade if trade and trade.get("status") == "open" else None
    trades = live_ledger.get_open_trades(source=source)
    return trades[0] if trades else None


def build_from_trade(trade: dict[str, Any]) -> Suggestion:
    """A card off a real open position, levels untouched."""
    import json

    def _levels(raw: Any) -> list[float]:
        try:
            return [float(x) for x in json.loads(raw or "[]")]
        except (TypeError, ValueError):
            return []

    buy = str(trade.get("side", "")).lower() in ("long", "buy")
    # Prefer the original plan: the stored list shrinks as targets fill.
    tps = (_levels(trade.get("plan_take_profits_json"))
           or _levels(trade.get("take_profits_json")))
    entry = float(trade["entry"])
    stop = float(trade.get("initial_stop_loss") or trade["stop_loss"])

    rationale = _original_rationale(trade) or (
        f"Mirrors live {trade.get('source', 'hq')} position "
        f"#{trade.get('id')} on {trade.get('product_id')}."
    )
    rr = abs(tps[0] - entry) / abs(entry - stop) if tps and entry != stop else None

    return Suggestion(
        action="spot_buy" if buy else "spot_sell",
        size=0.0,
        entry=round(entry, 2),
        stop_loss=round(stop, 2),
        take_profits=[round(t, 2) for t in tps],
        risk_reward=round(rr, 2) if rr else None,
        rationale=f"DEMO — mirrors real open trade #{trade.get('id')}.\n\n{rationale}",
        product_id=str(trade["product_id"]),
    )


def send(telegram_id: int, *, product: str = "BTC-USD",
         side: str = "buy", mirror: bool = False,
         source: str | None = None,
         trade_id: int | None = None) -> dict[str, Any]:
    """Build and DM one demo card. Returns what happened; never raises."""
    import display_summary
    import notify
    import pool
    import research
    import telegram_ui

    if not bot_config.POOL_ENABLED:
        return {"ok": False, "reason": "pool_disabled"}
    if not pool.is_approved(telegram_id):
        return {"ok": False, "reason": "not_approved"}

    trade = None
    if mirror:
        trade = pick_trade(trade_id, source)
        if trade is None:
            return {"ok": False, "reason": "no_open_trade"}
        suggestion = build_from_trade(trade)
        product = str(trade["product_id"])
        side = "buy" if suggestion.action == "spot_buy" else "sell"
        banner = MIRROR_BANNER.format(
            label=f"{trade.get('source', 'hq')} #{trade.get('id')}"
        )
    else:
        banner = BANNER

    # Read spot either way: for a synthetic card it sets the levels, for a
    # mirror it says how far the market has moved off that entry.
    try:
        spot = float(research.get_spot_price(product_id=product) or 0)
    except Exception:
        logger.exception("demo card: spot read failed")
        spot = 0.0
    if trade is None:
        if spot <= 0:
            return {"ok": False, "reason": "no_spot"}
        suggestion = build_suggestion(product, side, spot)

    token = uuid.uuid4().hex[:12]
    try:
        body = display_summary.build_card_body(
            suggestion, telegram_id=telegram_id,
            spot=spot or None,
        )
    except Exception:
        logger.exception("demo card: render failed")
        return {"ok": False, "reason": "render_failed"}

    prosp = pool.prospective_accept(
        telegram_id, entry=float(suggestion.entry),
        stop_loss=float(suggestion.stop_loss),
    )

    sent = notify.send_pool_dm_with_keyboard(
        telegram_id, f"{banner}\n\n{body}"[:4096],
        telegram_ui.pool_demo_keyboard(token),
    )
    if not sent:
        return {"ok": False, "reason": "send_failed"}

    entry = float(suggestion.entry)
    return {
        "ok": True, "ref": f"{REF_PREFIX}{token}", "product": product,
        "side": side, "spot": spot, "entry": entry,
        "stop_loss": float(suggestion.stop_loss),
        "take_profits": list(suggestion.take_profits or []),
        "risk_usd": float(prosp.get("risk_usd") or 0.0),
        "notional_usd": float(prosp.get("notional_usd") or 0.0),
        "quotes_a_size": bool(prosp.get("ok")),
        "mirrored_trade_id": trade.get("id") if trade else None,
        "mirrored_source": trade.get("source") if trade else None,
        "drift_pct": (abs(spot - entry) / entry * 100.0
                      if trade and spot > 0 and entry else None),
    }
