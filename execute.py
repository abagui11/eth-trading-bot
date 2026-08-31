"""Live execution for HQ ICT and mill ideas — Coinbase US futures (CDE).

Fills route to CFTC-regulated CDE nano futures via the Advanced Trade REST
API (see coinbase_deriv.py). Orders are whole contracts (0.1 ETH / 0.01 BTC),
so actual filled size can be smaller than the requested notional.

Design rules (non-negotiable):
  - ICT propose → validate → critic → stop math is UNCHANGED. This module only
    changes the fill destination and the live sizing percentage.
  - Every live entry immediately places a reduce-only stop-market on the
    exchange. If the stop is rejected → flatten the position and halt live.
  - Kill switches: daily realized loss ≥ LIVE_DAILY_LOSS_LIMIT_USD halts HQ
    until the next UTC day; max LIVE_MAX_OPEN_HQ concurrent HQ positions (new
    ideas are skipped, never FIFO-killed); scale-ins are paper-only.
  - EXECUTION_MODE=off → no-op. shadow → log the exact order, place nothing.
    live → real orders.

CLI smoke test (run before any live capital):
    python execute.py smoke              # auth + instruments + account summary
    python execute.py smoke --order      # + $10 ETH test order + stop + flatten
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import bot_config
import config
import live_ledger
from coinbase_deriv import INSTRUMENT_MAP, GatewayError, get_gateway
from models import Suggestion

logger = logging.getLogger(__name__)

_HALT_KEY = "live_halt"                  # '' | reason string
_HALT_DATE_KEY = "live_halt_date"        # UTC date the halt applies to
_MILL_FILLS_KEY = "mill_fills_date"      # 'YYYY-MM-DD:count'


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Halt / kill-switch state
# ---------------------------------------------------------------------------

def is_halted() -> str | None:
    """Return the halt reason when live trading is halted for today."""
    reason = live_ledger.get_meta(_HALT_KEY)
    if not reason:
        return None
    halt_date = live_ledger.get_meta(_HALT_DATE_KEY)
    if halt_date and halt_date < _today():
        # Daily halts expire at the next UTC day; manual halts persist.
        if reason.startswith("daily_loss"):
            clear_halt()
            return None
    return reason


def halt_live(reason: str) -> None:
    already = live_ledger.get_meta(_HALT_KEY)
    live_ledger.set_meta(_HALT_KEY, reason)
    live_ledger.set_meta(_HALT_DATE_KEY, _today())
    logger.error("LIVE HALT: %s", reason)
    if already != reason:
        _notify_ops(f"LIVE HALT — {reason}. New live entries paused.")


def clear_halt() -> None:
    live_ledger.set_meta(_HALT_KEY, "")
    live_ledger.set_meta(_HALT_DATE_KEY, "")


def _realized_pnl_today(source: str) -> float:
    total = 0.0
    for t in live_ledger.get_closed_trades(limit=200, source=source):
        if (t.get("closed_at") or "").startswith(_today()):
            total += float(t.get("pnl_usd") or 0.0)
    return total


def _check_daily_loss(source: str) -> bool:
    """True when the sleeve may trade; halts HQ when the day is blown."""
    limit = (
        bot_config.LIVE_DAILY_LOSS_LIMIT_USD
        if source == "hq"
        else bot_config.LIVE_MILL_DAILY_LOSS_LIMIT_USD
    )
    pnl = _realized_pnl_today(source)
    if pnl <= -limit:
        halt_live(f"daily_loss:{source}:{pnl:.2f}")
        return False
    return True


# ---------------------------------------------------------------------------
# Sizing (live-only — paper caps untouched)
# ---------------------------------------------------------------------------

def _mill_clip(product_id: str, price: float) -> tuple[float, float] | None:
    """Mill target notional → (qty, notional), rounded UP to one nano contract.

    CDE orders fill in whole contracts, so a target below one contract cannot
    execute at all. Rounding up rather than skipping is what keeps a clip
    fillable once price rises past target / contract_floor — at ETH $3,000 a
    $260 target is 0.087 ETH, under the 0.1 floor, and used to abort the fill.
    The caller still checks the resulting notional against the sleeve, so an
    oversized contract (BTC at a high print) is rejected there rather than here.
    """
    if price <= 0:
        return None
    qty = bot_config.LIVE_MILL_NOTIONAL_USD / price
    floor = bot_config.LIVE_PRODUCT_QTY_FLOORS.get(product_id)
    if floor is not None and qty < floor:
        qty = float(floor)
    qty = round(qty, 6)
    if qty <= 0:
        return None
    return qty, qty * price


# ---------------------------------------------------------------------------
# Core entry
# ---------------------------------------------------------------------------

def maybe_execute_live(
    suggestion: Suggestion,
    spot_price: float,
    *,
    cycle_id: str | None,
    source: str = "hq",
    fill_type: str = "auto",
    filled_by: int | None = None,
) -> dict[str, Any] | None:
    """Mirror a validated fill onto Coinbase perps. Never raises into the
    paper path — all failures log, halt if needed, and return None."""
    try:
        return _execute(
            suggestion,
            spot_price,
            cycle_id=cycle_id,
            source=source,
            fill_type=fill_type,
            filled_by=filled_by,
        )
    except Exception:
        logger.exception("Live execution error (%s %s)", source, cycle_id)
        return None


def _execute(
    suggestion: Suggestion,
    spot_price: float,
    *,
    cycle_id: str | None,
    source: str,
    fill_type: str = "auto",
    filled_by: int | None = None,
) -> dict[str, Any] | None:
    mode = config.EXECUTION_MODE
    if mode == "off":
        return None
    if suggestion.action not in ("deriv_buy", "deriv_sell", "spot_buy", "spot_sell"):
        return None
    if suggestion.stop_loss is None:
        logger.warning("Live skip: suggestion has no stop_loss (%s)", cycle_id)
        return None

    reason = is_halted()
    if reason:
        logger.warning("Live skip: halted (%s)", reason)
        return None
    if not _check_daily_loss(source):
        return None

    # Scale-ins are paper-only on live: at 50% deploy an add would put 100%
    # of the sleeve into one idea.
    if not bot_config.LIVE_SCALE_IN_ENABLED and suggestion.entry_tranche == str(
        bot_config.ADD_FIB_LEVEL
    ):
        logger.info("Live skip: scale-in tranche (paper-only) %s", cycle_id)
        return None

    product_id = suggestion.product_id
    instrument = INSTRUMENT_MAP.get(product_id)
    if not instrument:
        logger.warning("Live skip: no instrument mapping for %s", product_id)
        return None

    side = "long" if suggestion.action in ("deriv_buy", "spot_buy") else "short"
    entry = suggestion.entry or spot_price
    ob_ref = suggestion.order_block_ref

    if source == "hq":
        import vault

        cid = str(cycle_id or "")
        alloc = vault.get_by_cycle(cid) if cid else None
        if alloc is None:
            alloc = vault.take(
                suggestion,
                cycle_id=cid or f"hq-{_today()}",
                spot=spot_price,
            )
        if not alloc.get("admitted"):
            logger.info("Vault skip: %s", alloc.get("skip_reason"))
            return None
        notional = float(alloc["notional_usd"])
        qty = float(alloc["qty"] or 0)
        if qty <= 0:
            return None
        open_trades = live_ledger.get_open_trades(source="hq")
        if ob_ref and any((t.get("notes") or "") == f"ob:{ob_ref}" for t in open_trades):
            logger.info("Live skip: already live on OB %s", ob_ref)
            return None
    else:
        open_trades = live_ledger.get_open_trades(source=source)
        max_open = bot_config.LIVE_MILL_MAX_OPEN
        if len(open_trades) >= max_open:
            logger.info("Live skip: %s sleeve full (%d open)", source, len(open_trades))
            return None

        if ob_ref and any((t.get("notes") or "") == f"ob:{ob_ref}" for t in open_trades):
            logger.info("Live skip: already live on OB %s", ob_ref)
            return None

        cap = bot_config.LIVE_MILL_MAX_FILLS_PER_DAY
        if cap > 0:
            fills = live_ledger.get_meta(_MILL_FILLS_KEY) or ""
            date, _, count_raw = fills.partition(":")
            count = int(count_raw) if date == _today() and count_raw.isdigit() else 0
            if count >= cap:
                logger.info("Live skip: mill daily fill cap reached")
                return None

        clip = _mill_clip(product_id, entry)
        if clip is None:
            return None
        qty, notional = clip
        sleeve = bot_config.LIVE_MILL_SLEEVE_USD
        open_notional = sum(
            float(t["qty"]) * float(t["entry"]) for t in open_trades
        )
        if open_notional + notional > sleeve * bot_config.LIVE_MAX_LEVERAGE:
            logger.info(
                "Live skip: %s exposure %.0f + %.0f exceeds %.0f×%.1fx",
                source, open_notional, notional, sleeve, bot_config.LIVE_MAX_LEVERAGE,
            )
            return None

    order_payload = {
        "instrument": instrument,
        "side": "buy" if side == "long" else "sell",
        "qty": qty,
        "entry_ref": entry,
        "stop_loss": suggestion.stop_loss,
        "take_profits": suggestion.take_profits,
        "notional_usd": notional,
        "source": source,
        "cycle_id": cycle_id,
        "fill_type": fill_type,
        "filled_by": filled_by,
    }

    if mode == "shadow":
        logger.info("SHADOW ORDER %s", json.dumps(order_payload, default=str))
        return {"mode": "shadow", **order_payload}

    # ---- live ----
    gw = get_gateway()
    try:
        order = gw.place_market_order(
            instrument=instrument,
            side="buy" if side == "long" else "sell",
            amount=qty,
            label=f"{source}:{cycle_id or 'manual'}",
        )
    except GatewayError as exc:
        logger.error("Live entry rejected: %s", exc)
        return None

    order_info = (order or {}).get("order") or {}
    fill_price = float(order_info.get("average_price") or entry)
    order_id = str(order_info.get("order_id") or "")
    # Contract flooring means the real fill can be smaller than requested —
    # stop and ledger must use the actual filled quantity.
    qty = float(order_info.get("filled_qty") or qty)

    # Stop first, questions later: reject → flatten + halt.
    try:
        stop = gw.place_stop_market(
            instrument=instrument,
            side="sell" if side == "long" else "buy",
            amount=qty,
            trigger_price=float(suggestion.stop_loss),
            label=f"{source}-stop:{cycle_id or 'manual'}",
        )
        stop_order_id = str(((stop or {}).get("order") or {}).get("order_id") or "")
    except GatewayError as exc:
        logger.error("STOP REJECTED — flattening and halting live: %s", exc)
        try:
            gw.close_position(instrument)
        except GatewayError:
            logger.exception("Flatten after stop-reject ALSO failed — manual action required")
        halt_live(
            f"stop_reject:{instrument}:{exc} — position flattened, verify on Coinbase"
        )
        return None

    trade_id = live_ledger.record_open(
        cycle_id=cycle_id,
        source=source,
        product_id=product_id,
        instrument=instrument,
        side=side,
        qty=qty,
        entry=fill_price,
        stop_loss=float(suggestion.stop_loss),
        take_profits_json=json.dumps(suggestion.take_profits or []),
        order_id=order_id or None,
        stop_order_id=stop_order_id or None,
        notes=f"ob:{ob_ref}" if ob_ref else None,
        fill_type=fill_type,
        filled_by=filled_by,
    )

    if source == "mill":
        fills = live_ledger.get_meta(_MILL_FILLS_KEY) or ""
        date, _, count_raw = fills.partition(":")
        count = int(count_raw) if date == _today() and count_raw.isdigit() else 0
        live_ledger.set_meta(_MILL_FILLS_KEY, f"{_today()}:{count + 1}")

    logger.info(
        "LIVE FILL #%d %s %s %s %s qty=%.6f @ %.2f stop=%.2f",
        trade_id, source, fill_type, instrument, side, qty, fill_price,
        suggestion.stop_loss,
    )
    if bot_config.LIVE_FILL_ALERTS_ENABLED:
        lines = [
            f"LIVE FILL #{trade_id} — {source} ({fill_type})",
            f"{product_id} {side} {qty:.4f} @ {fill_price:,.2f}",
            f"stop {float(suggestion.stop_loss):,.2f}",
        ]
        if filled_by:
            lines.append(f"accepted by {filled_by}")
        status = _sleeve_status(source)
        if status:
            lines.append(status)
        _notify_ops("\n".join(lines))
    return {"mode": "live", "trade_id": trade_id, **order_payload, "fill": fill_price}


# ---------------------------------------------------------------------------
# Mill sleeve — capacity and the two entry paths (auto FIFO, manual Accept)
# ---------------------------------------------------------------------------

def mill_capacity() -> dict[str, Any]:
    """Live mill sleeve occupancy. Safe to call from the bot's Telegram path."""
    try:
        open_trades = live_ledger.get_open_trades(source="mill")
    except Exception:
        logger.exception("mill_capacity read failed")
        open_trades = []
    open_notional = sum(
        float(t["qty"]) * float(t["entry"]) for t in open_trades
    )
    max_open = bot_config.LIVE_MILL_MAX_OPEN
    sleeve = bot_config.LIVE_MILL_SLEEVE_USD
    return {
        "open": len(open_trades),
        "max_open": max_open,
        "slots_free": max(max_open - len(open_trades), 0),
        "open_notional_usd": round(open_notional, 2),
        "sleeve_usd": sleeve,
        "sleeve_free_usd": round(
            max(sleeve * bot_config.LIVE_MAX_LEVERAGE - open_notional, 0.0), 2
        ),
        "halted": is_halted(),
        "open_trades": [
            {
                "id": t["id"],
                "product_id": t["product_id"],
                "side": t["side"],
                "entry": t["entry"],
                "fill_type": t.get("fill_type") or "auto",
                "opened_at": t["opened_at"],
            }
            for t in open_trades
        ],
    }


def execute_mill_idea(
    *,
    idea_id: int,
    product_id: str,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profits: list[float] | None = None,
    signal_key: str | None = None,
    confidence: float | None = None,
    fill_type: str = "auto",
    accepted_by: int | None = None,
) -> dict[str, Any]:
    """Single gate for both mill entry paths. Returns a structured verdict.

    ``auto`` is the always-on FIFO path: it fills only while the sleeve is
    EMPTY, and only above the conviction floor, so it can never consume the
    slots reserved for operator Accepts. ``manual`` comes from an Accept by a
    LIVE_MILL_FILL_TELEGRAM_IDS operator and skips the conviction gate, but
    still respects the open-count, sleeve, and halt limits.

    ``skip_reason`` is the contract the Telegram reply is built from — in
    particular ``sleeve_full`` is what tells an operator there are too many
    trades already open.
    """
    capacity = mill_capacity()
    verdict: dict[str, Any] = {
        "executed": False,
        "skip_reason": None,
        "fill_type": fill_type,
        "capacity": capacity,
        "execution_mode": config.EXECUTION_MODE,
        "result": None,
    }

    def _skip(reason: str) -> dict[str, Any]:
        verdict["skip_reason"] = reason
        logger.info("Mill idea #%s not filled (%s): %s", idea_id, fill_type, reason)
        return verdict

    if direction not in ("long", "short"):
        return _skip("bad_direction")

    if fill_type == "manual":
        if accepted_by is None or int(accepted_by) not in tuple(
            bot_config.LIVE_MILL_FILL_TELEGRAM_IDS
        ):
            return _skip("not_authorized")
    elif fill_type == "auto":
        if not bot_config.LIVE_MILL_AUTO_FILL_ENABLED:
            return _skip("auto_disabled")
        if confidence is None or float(confidence) < (
            bot_config.LIVE_MILL_AUTO_MIN_CONFIDENCE
        ):
            return _skip("low_conviction")
        # FIFO: the auto path exists only to guarantee the book is never
        # empty. Once anything is open, later slots belong to manual Accepts.
        if capacity["open"] > 0:
            return _skip("book_not_empty")
    else:
        return _skip("bad_fill_type")

    if capacity["halted"]:
        return _skip("halted")
    if capacity["slots_free"] <= 0:
        return _skip("sleeve_full")

    suggestion = Suggestion(
        action="deriv_buy" if direction == "long" else "deriv_sell",
        size=0.0,  # live sizing comes from the mill clip, not paper size
        entry=entry,
        stop_loss=stop_loss,
        take_profits=list(take_profits or []),
        risk_reward=None,
        rationale=f"mill idea #{idea_id}",
        product_id=product_id,
        # Dedupe key: one live clip per mill idea, mirroring the OB rule.
        order_block_ref=signal_key or f"mill-idea-{idea_id}",
    )
    result = maybe_execute_live(
        suggestion,
        entry,
        cycle_id=f"mill_{idea_id}",
        source="mill",
        fill_type=fill_type,
        filled_by=accepted_by,
    )
    if result is None:
        # execute.py logs the specific reason (exposure, floor, dedupe, mode).
        return _skip("rejected")
    verdict["executed"] = True
    verdict["result"] = result
    verdict["capacity"] = mill_capacity()
    return verdict


# ---------------------------------------------------------------------------
# Reconciliation — detect exchange-side closes (stop fills, manual flattens)
# ---------------------------------------------------------------------------

def sync_live_positions() -> None:
    """Mark ledger trades closed when the exchange no longer holds them.
    Called periodically (watchdog loop). Best-effort; never raises."""
    if config.EXECUTION_MODE != "live":
        return
    open_trades = live_ledger.get_open_trades()
    if not open_trades:
        return
    try:
        gw = get_gateway()
        for instrument in {t["instrument"] for t in open_trades}:
            pos = gw.get_position(instrument)
            size = abs(float((pos or {}).get("size") or 0.0))
            if size > 0:
                continue
            mark = float((pos or {}).get("mark_price") or 0.0)
            for t in open_trades:
                if t["instrument"] != instrument:
                    continue
                exit_price = mark or float(t.get("stop_loss") or t["entry"])
                qty = float(t["qty"])
                direction = 1.0 if t["side"] == "long" else -1.0
                pnl = (exit_price - float(t["entry"])) * qty * direction
                live_ledger.record_close(
                    int(t["id"]),
                    exit_price=exit_price,
                    pnl_usd=pnl,
                    close_reason="exchange_close",
                )
                logger.info(
                    "Live trade #%s closed on exchange (%.2f, pnl %.2f)",
                    t["id"], exit_price, pnl,
                )
                if bot_config.LIVE_FILL_ALERTS_ENABLED:
                    _notify_ops(
                        f"LIVE CLOSE #{t['id']} — {t['source']}\n"
                        f"{t['product_id']} {t['side']} {qty:.4f}\n"
                        f"entry {float(t['entry']):,.2f} -> exit {exit_price:,.2f}\n"
                        f"P&L {pnl:+,.2f} (exchange close / stop)"
                    )
            _check_daily_loss("hq")
            _check_daily_loss("mill")
    except Exception:
        logger.exception("sync_live_positions failed")


def _sleeve_status(source: str) -> str:
    """Capacity line for fill alerts. Cosmetic — swallow any read failure."""
    try:
        if source == "mill":
            cap = mill_capacity()
            return (
                f"sleeve {cap['open']}/{cap['max_open']} open · "
                f"${cap['open_notional_usd']:,.0f} of ${cap['sleeve_usd']:,.0f}"
            )
        open_n = len(live_ledger.get_open_trades(source=source))
        return f"sleeve {open_n}/{bot_config.LIVE_MAX_OPEN_HQ} open"
    except Exception:
        logger.exception("Sleeve status for alert failed (%s)", source)
        return ""


def _alert_chat_ids() -> list[str]:
    """Admin chat plus the named operators, de-duped, order preserved."""
    ids = [config.TELEGRAM_ADMIN_CHAT_ID or config.TELEGRAM_CHAT_ID]
    ids += [str(i) for i in bot_config.LIVE_ALERT_TELEGRAM_IDS]
    seen: set[str] = set()
    out: list[str] = []
    for raw in ids:
        chat = str(raw or "").strip()
        if chat and chat not in seen:
            seen.add(chat)
            out.append(chat)
    return out


def _notify_ops(message: str) -> None:
    """Best-effort Telegram + email to ops; never raises."""
    try:
        import requests as _rq

        for chat in _alert_chat_ids():
            try:
                _rq.post(
                    f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat, "text": message},
                    timeout=10,
                )
            except Exception:
                # One unreachable operator must not silence the others.
                logger.exception("Ops Telegram notify failed for chat %s", chat)
    except Exception:
        logger.exception("Ops Telegram notify failed")
    try:
        import requests as _rq

        if config.RESEND_API_KEY and config.ALERT_EMAIL_TO:
            _rq.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
                json={
                    "from": config.ALERT_EMAIL_FROM,
                    "to": [config.ALERT_EMAIL_TO],
                    "subject": "LIVE EXECUTION ALERT",
                    "text": message,
                },
                timeout=10,
            )
    except Exception:
        logger.exception("Ops email notify failed")


# ---------------------------------------------------------------------------
# Smoke test CLI
# ---------------------------------------------------------------------------

def smoke_test(place_order: bool = False) -> int:
    """Auth → instruments → balances; optionally a 1-contract round-trip."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    gw = get_gateway()

    print(f"Gateway: {gw.base_url}")
    print("1) key_permissions auth check…", end=" ")
    gw.auth_check()
    print("OK")

    print("2) futures products…", end=" ")
    instruments = gw.get_instruments()
    names = {i.get("instrument_name") for i in instruments}
    for want in INSTRUMENT_MAP.values():
        status = "OK" if want in names else "MISSING"
        print(f"\n   {want}: {status}", end="")
    print()

    print("3) futures balance summary…", end=" ")
    summary = gw.get_account_summary()
    print(
        f"OK — equity {summary.get('equity')} USD, "
        f"buying power {summary.get('available_funds')}"
    )

    if not place_order:
        print("\nDry smoke passed. Re-run with --order for a 1-contract round-trip.")
        return 0

    # Smallest possible live test: one nano ETH contract (0.1 ETH ≈ $250).
    instrument = INSTRUMENT_MAP["ETH-USD"]
    ticker = gw.get_ticker(instrument)
    mark = float(ticker.get("mark_price") or 0)
    if mark <= 0:
        print("No mark price — aborting order test.")
        return 1
    qty = gw.contract_size(instrument)
    stop_px = round(mark * 0.98, 2)
    print(f"4) 1-contract test: buy {qty} {instrument} @ market (mark {mark})")
    gw.place_market_order(
        instrument=instrument, side="buy", amount=qty, label="smoke-entry"
    )
    print(f"5) resting stop @ {stop_px}")
    stop = gw.place_stop_market(
        instrument=instrument,
        side="sell",
        amount=qty,
        trigger_price=stop_px,
        label="smoke-stop",
    )
    stop_id = str(((stop or {}).get("order") or {}).get("order_id") or "")
    print("6) cancel stop + flatten")
    # Cancel by id — a just-placed order may not appear in the open-orders
    # listing yet (eventual consistency), so cancel_all alone can miss it.
    if stop_id:
        gw.cancel_orders([stop_id])
    gw.cancel_all_by_instrument(instrument)
    gw.close_position(instrument)
    import time as _time

    for _ in range(5):  # verify nothing is left resting
        _time.sleep(1.0)
        leftovers = gw.cancel_all_by_instrument(instrument)
        if not leftovers:
            break
        print(f"   cancelled {len(leftovers)} leftover order(s)")
    print("Smoke round-trip complete — verify a flat book on Coinbase.")
    return 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "smoke":
        live_ledger.init_db()
        raise SystemExit(smoke_test(place_order="--order" in sys.argv))
    print(__doc__)
