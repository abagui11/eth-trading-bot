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

LIVE_BANNER = (
    "LIVE CARD — this is mill idea #{idea_id}, not a demo. Accept places a "
    "real trade with real money at the size shown below. It is not a promise "
    "of a fill: the levels are re-checked against the market at the moment "
    "you tap, and a setup the price has left behind is refused."
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
EVERYONE = ("all", "everyone", "broadcast")
# Send a real, fillable mill card instead of a demo one. Deliberately not a
# synonym of `live` -- `/democard live` already means "mirror an open
# position", and quietly changing that into "spend money" is not a thing to do
# with a word someone has already been using.
REAL = ("real", "fillable", "forreal")
# Report what is fillable without sending anything. The answer decides whether
# a live card can be shown at all, and before this it needed an SSH session
# (`deploy/_show_fillable.py`) -- which is the one thing the command exists to
# avoid having in the shot.
SCAN = ("scan", "check", "what")


def parse_args(args: list[str], *, default_id: int) -> dict[str, Any]:
    """Read `/democard [id|all] [live|mill|hq|real|scan] [product] [side] [#]` in any order.

    Order-insensitive on purpose: this gets typed mid-recording, and having to
    remember positions is exactly when it gets typed wrong. Telegram ids are
    long, trade ids are short, which is what separates `85` from `8708390551`.
    """
    out: dict[str, Any] = {
        "telegram_id": default_id, "product": "BTC-USD", "side": "buy",
        "mirror": False, "source": None, "trade_id": None, "everyone": False,
        "live_idea": False, "idea_id": None, "scan": False,
    }
    for raw in args:
        token = str(raw).strip().lower().lstrip("-#")
        if token in EVERYONE:
            out["everyone"] = True
        elif token in SCAN:
            out["scan"] = True
        elif token in REAL:
            out["live_idea"] = True
        elif token in MIRRORS:
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

    # A short number means "that mill idea" once `real` is in play, because on
    # that path there is nothing to mirror -- the card *is* the idea. Resolved
    # after the loop so `real 85` and `85 real` read the same, which is the
    # whole point of parsing this order-insensitively.
    if out["live_idea"] and out["trade_id"] is not None:
        out["idea_id"] = out["trade_id"]
        out["trade_id"] = None
        out["mirror"] = False
    return out


def recipients() -> list[int]:
    """Every approved pool account — who a real card would reach."""
    import pool

    return [
        int(a["telegram_id"]) for a in pool.list_accounts()
        if pool.is_approved(int(a["telegram_id"]))
    ]


def send_many(telegram_ids: list[int], **kwargs: Any) -> dict[str, Any]:
    """Fan one demo card out, sized per recipient.

    Not one rendered card reused: each is built for its own account, because
    the size line is personal and a shared render would show everyone the same
    number. This is also how the real broadcast behaves.
    """
    sent: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for uid in telegram_ids:
        result = send(int(uid), **kwargs)
        (sent if result.get("ok") else failed).append({"telegram_id": uid, **result})
    return {"sent": sent, "failed": failed}


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


def _verdict_line(row: dict[str, Any]) -> dict[str, Any]:
    """One idea's fill verdict, in words rather than skip codes."""
    import trade_ideas_bridge as bridge

    preview = row.get("preview") or {}
    fills = bool(row.get("would_fill"))
    return {
        "id": int(row["id"]),
        "product_id": row.get("product_id"),
        "direction": row.get("direction"),
        "status": row.get("status"),
        "would_fill": fills,
        "skip_reason": preview.get("skip_reason"),
        "why": "" if fills else bridge.explain_skip(preview.get("skip_reason")),
        "born_rr": preview.get("born_rr"),
    }


def find_live_idea(
    user_id: int, *, idea_id: int | None = None, limit: int = 30
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The mill idea to send live, plus the verdict on everything considered.

    A demo card cannot fill — its ref matches no executor — so asking for a
    genuine fill means sending a *real* card instead. This picks one that is
    not already refusable, which is a strong no and a weak yes: the final
    exposure, contract-floor and dedupe checks only run when an order is
    actually sent.

    The verdicts come back either way. When nothing is fillable the caller can
    then say *which* ideas were looked at and why each was refused, instead of
    a bare "nothing right now" that leaves someone about to record wondering
    whether the mill is broken or the market simply moved.
    """
    import trade_ideas_bridge as bridge

    if idea_id is not None:
        verdict = bridge.preview_fill(int(idea_id), user_id)
        row = dict(bridge._idea_row(int(idea_id)) or {"id": int(idea_id)})
        row.update({"preview": verdict, "would_fill": bool(verdict.get("would_fill"))})
        considered = [_verdict_line(row)]
        return (row if row["would_fill"] else None), considered

    rows = bridge.fillable_ideas(user_id, limit=limit)
    picked = next((r for r in rows if r.get("would_fill")), None)
    return picked, [_verdict_line(r) for r in rows]


def pick_fillable_idea(user_id: int, *, limit: int = 30) -> dict[str, Any] | None:
    """The newest mill idea that would fill for this user right now."""
    return find_live_idea(user_id, limit=limit)[0]


def scan_ideas(user_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
    """Fill verdicts for the newest mill ideas — a pre-flight, sends nothing."""
    return find_live_idea(user_id, limit=limit)[1]


def build_from_idea(idea: dict[str, Any]) -> Suggestion:
    """A card off a live mill idea, levels untouched."""
    import json

    import trade_ideas_bridge as bridge

    row = bridge._idea_row(int(idea["id"])) or idea
    long = str(row.get("direction") or "") == "long"
    try:
        tps = [float(x) for x in json.loads(row.get("take_profits_json") or "[]")]
    except (TypeError, ValueError):
        tps = []
    return Suggestion(
        action="spot_buy" if long else "spot_sell",
        size=0.0,
        entry=float(row["entry"]),
        stop_loss=float(row["stop_loss"]),
        take_profits=tps,
        rationale=str(row.get("title") or f"mill idea #{row['id']}"),
        product_id=str(row["product_id"]),
    )


def send(telegram_id: int, *, product: str = "BTC-USD",
         side: str = "buy", mirror: bool = False,
         source: str | None = None,
         trade_id: int | None = None,
         live_idea: bool = False,
         idea_id: int | None = None) -> dict[str, Any]:
    """Build and DM one card. Returns what happened; never raises.

    `live_idea` sends a **real** mill card rather than a demo one: real levels,
    the real Accept callback, and therefore a real trade if it is tapped. The
    banner says so. A card that spends money must never be labelled a demo —
    that is the one combination worse than either on its own. `idea_id` aims
    that at one specific mill idea instead of the newest fillable one; it is
    still put through the same gate, so naming an idea asks for it rather than
    forces it.
    """
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
    idea = None
    if live_idea:
        idea, considered = find_live_idea(telegram_id, idea_id=idea_id)
        if idea is None:
            return {
                "ok": False,
                "reason": ("idea_not_fillable" if idea_id is not None
                           else "nothing_fillable"),
                "idea_id": idea_id,
                "considered": considered,
            }
        suggestion = build_from_idea(idea)
        product = str(suggestion.product_id)
        side = "buy" if suggestion.action == "spot_buy" else "sell"
        banner = LIVE_BANNER.format(idea_id=idea["id"])
    elif mirror:
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
    if trade is None and idea is None:
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

    keyboard = (
        telegram_ui.idea_live_keyboard(int(idea["id"])) if idea is not None
        else telegram_ui.pool_demo_keyboard(token)
    )
    sent = notify.send_pool_dm_with_keyboard(
        telegram_id, f"{banner}\n\n{body}"[:4096], keyboard,
    )
    if not sent:
        return {"ok": False, "reason": "send_failed"}

    entry = float(suggestion.entry)
    if idea is not None:
        return {
            "ok": True, "live": True, "idea_id": int(idea["id"]),
            "ref": f"mill_{int(idea['id'])}", "product": product, "side": side,
            "spot": spot, "entry": entry,
            "stop_loss": float(suggestion.stop_loss),
            "take_profits": list(suggestion.take_profits or []),
            "risk_usd": float(prosp.get("risk_usd") or 0.0),
            "notional_usd": float(prosp.get("notional_usd") or 0.0),
            "quotes_a_size": bool(prosp.get("ok")),
            "born_rr": (idea.get("preview") or {}).get("born_rr"),
        }
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
