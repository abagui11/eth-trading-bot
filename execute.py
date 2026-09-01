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
# Exit ladder — take-profits are exchange-resting, not just ledger metadata
# ---------------------------------------------------------------------------

def _ordered_tps(side: str, take_profits: list[float] | None, entry: float) -> list[float]:
    """Targets in the order price would reach them, wrong-side levels dropped."""
    levels = [float(tp) for tp in (take_profits or []) if tp]
    if side == "long":
        return sorted(tp for tp in levels if tp > entry)
    return sorted((tp for tp in levels if tp < entry), reverse=True)


def _tp_ladder(contracts: int, levels: list[float]) -> list[tuple[float, int]]:
    """Whole-contract scale-out plan as ``(price, contracts)`` rungs.

    Contracts are indivisible, so a clip with fewer contracts than targets
    can't use the whole ladder. It banks at the *nearest* targets rather than
    skipping early profit — a one-contract clip closes fully at TP1.
    """
    if contracts <= 0 or not levels:
        return []
    used = levels[: min(contracts, len(levels))]
    rungs = len(used)
    base, extra = divmod(contracts, rungs)
    # The remainder rides the furthest targets, so the runner is the last rung.
    return [
        (price, base + (1 if i >= rungs - extra else 0))
        for i, price in enumerate(used)
    ]


def _tp_reached(side: str, price: float, level: float) -> bool:
    return price >= level if side == "long" else price <= level


def _near_level(price: float, level: float, *, rel: float = 0.0025) -> bool:
    band = max(1.0, abs(float(level)) * rel)
    return abs(float(price) - float(level)) <= band


def _bracket_triggers(order: dict[str, Any]) -> tuple[float | None, float | None]:
    cfg = (order or {}).get("order_configuration") or {}
    br = cfg.get("trigger_bracket_gtc") or {}
    limit = float(br.get("limit_price") or 0) or None
    stop = float(br.get("stop_trigger_price") or 0) or None
    return limit, stop


def _is_plain_stop_order(order: dict[str, Any]) -> bool:
    cfg = (order or {}).get("order_configuration") or {}
    return any("stop" in str(k).lower() and "bracket" not in str(k).lower() for k in cfg)


def _classify_fill_reason(
    order: dict[str, Any],
    *,
    side: str,
    entry: float,
    price: float,
    take_profits: list[float] | None,
    stop_loss: float | None,
) -> str:
    """Take-profit vs stop from the order that filled, not from P&L sign.

    A stop that has trailed to breakeven or TP1 still books a profit. Tagging
    that remainder as take_profit lit TP3 on the card and counted an extra
    trail step, even though price never reached the last target.
    """
    limit, br_stop = _bracket_triggers(order)
    if limit and br_stop:
        if abs(price - br_stop) <= abs(price - limit):
            return "stop_loss"
        return "take_profit"
    if _is_plain_stop_order(order):
        return "stop_loss"
    ordered = _ordered_tps(side, take_profits, entry)
    if any(_near_level(price, level) for level in ordered):
        return "take_profit"
    if stop_loss is not None and _near_level(price, float(stop_loss)):
        return "stop_loss"
    direction = 1.0 if side == "long" else -1.0
    return "take_profit" if (price - entry) * direction > 0 else "stop_loss"


def arm_exits(
    gw: Any,
    *,
    instrument: str,
    side: str,
    qty: float,
    entry: float,
    stop_loss: float,
    take_profits: list[float] | None,
    label: str,
    mark: float | None = None,
) -> dict[str, Any]:
    """Put the exit plan on the exchange and report what it did.

    Every target rests as a bracket carrying both the take-profit and the stop,
    so a filled leg consumes its own protection. Targets already through the
    market are realized now rather than rested, mirroring the paper book's
    gap-through fills. Falls back to one plain stop for the whole position if
    brackets can't be placed — the position is never left unprotected.
    """
    closing = "sell" if side == "long" else "buy"
    result: dict[str, Any] = {
        "exit_order_ids": [],
        "realized": [],
        "mode": "brackets",
        "stop_order_id": None,
    }

    try:
        csize = gw.contract_size(instrument)
        contracts = int(round(qty / csize))
    except Exception:
        logger.exception("Exit ladder: contract sizing failed (%s)", instrument)
        contracts, csize = 0, 0.0

    ladder = _tp_ladder(contracts, _ordered_tps(side, take_profits, entry))
    if not ladder:
        logger.info("Exit ladder: no usable targets for %s — plain stop", label)
        return _arm_plain_stop(gw, instrument, closing, qty, stop_loss, label, result)

    if mark is None:
        try:
            mark = float((gw.get_position(instrument) or {}).get("mark_price") or 0)
        except Exception:
            mark = 0.0

    placed: list[str] = []
    try:
        for rung, (price, n) in enumerate(ladder, start=1):
            leg_qty = n * csize
            if mark and _tp_reached(side, mark, price):
                # Target already through: bank it now instead of resting an
                # order behind the market, exactly as the paper ladder does.
                fill = gw.place_market_order(
                    instrument=instrument,
                    side=closing,
                    amount=leg_qty,
                    label=f"{label}-tp@{price:g}",
                )
                info = (fill or {}).get("order") or {}
                result["realized"].append(
                    {
                        "order_id": str(info.get("order_id") or ""),
                        "qty": float(info.get("filled_qty") or leg_qty),
                        "price": float(info.get("average_price") or mark),
                        "target": price,
                    }
                )
                continue
            order = gw.place_bracket(
                instrument=instrument,
                side=closing,
                amount=leg_qty,
                limit_price=price,
                stop_trigger_price=stop_loss,
                label=f"{label}-tp{rung}",
            )
            oid = str(((order or {}).get("order") or {}).get("order_id") or "")
            if oid:
                placed.append(oid)
    except GatewayError as exc:
        logger.error("Bracket placement failed (%s) — reverting to plain stop: %s", label, exc)
        if placed:
            try:
                gw.cancel_orders(placed)
            except GatewayError:
                logger.exception("Could not cancel partial brackets for %s", label)
        result["exit_order_ids"] = []
        # A market leg can fail *after* executing (the confirming read 404s on a
        # just-placed order), so the size to protect comes from the exchange
        # rather than from what this function thinks it sold.
        remaining = qty - sum(r["qty"] for r in result["realized"])
        try:
            live_size = abs(float((gw.get_position(instrument) or {}).get("size") or 0.0))
            if live_size < remaining:
                logger.warning(
                    "%s: exchange holds %.4f, expected %.4f — protecting the "
                    "smaller size", label, live_size, remaining,
                )
                remaining = live_size
        except Exception:
            logger.exception("Could not re-read position for %s", label)
        if remaining <= 0:
            result["mode"] = "flat"
            return result
        return _arm_plain_stop(
            gw, instrument, closing, remaining, stop_loss, label, result
        )

    result["exit_order_ids"] = placed
    if not placed and result["realized"]:
        # Every rung was already through the market: nothing left to protect.
        result["mode"] = "flat"
    return result


def revalidate_levels(
    *,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profits: list[float],
    spot: float,
) -> dict[str, Any]:
    """Re-check an idea's plan against the price we would actually fill at.

    An idea is priced at mint and filled whenever someone taps Accept, so the
    market has usually moved in between. The entry always becomes the live
    mark, because that is what a market order gets; what happens to the rest
    depends on which way it moved:

    * price ran **against** the entry (a worse fill than planned) — the whole
      structure shifts with it, so the clip still risks the distance the plan
      called for instead of silently risking more. Past ``LIVE_MAX_CHASE_R``
      of that distance it is chasing, and is refused.
    * price came **toward** the entry (a better fill) — the structural stop
      and targets are kept, so the trade simply risks less for the same
      reward. Through the stop, the premise is dead.

    Returns ``{"ok": False, "reason": ...}`` or the re-anchored plan.
    """
    long = direction == "long"
    if spot <= 0:
        return {"ok": False, "reason": "no_mark"}

    risk_planned = (entry - stop_loss) if long else (stop_loss - entry)
    if risk_planned <= 0:
        return {"ok": False, "reason": "bad_levels"}

    chase = (spot - entry) if long else (entry - spot)
    if chase > risk_planned * bot_config.LIVE_MAX_CHASE_R:
        return {"ok": False, "reason": "chased", "chase_r": round(chase / risk_planned, 2)}

    if chase > 0:
        shift = spot - entry
        new_stop = stop_loss + shift
        tps = [float(tp) + shift for tp in (take_profits or [])]
    else:
        new_stop = float(stop_loss)
        tps = [float(tp) for tp in (take_profits or [])]

    risk_now = (spot - new_stop) if long else (new_stop - spot)
    if risk_now <= 0:
        return {"ok": False, "reason": "stop_breached"}

    edge = spot * (bot_config.LIVE_TP_MIN_EDGE_PCT / 100.0)
    ahead = sorted(
        (tp for tp in tps if (tp > spot + edge if long else tp < spot - edge)),
        reverse=not long,
    )
    if not ahead:
        return {"ok": False, "reason": "targets_passed"}

    avg = sum(ahead) / len(ahead)
    rr = ((avg - spot) if long else (spot - avg)) / risk_now
    if rr < bot_config.LIVE_MIN_FILL_RR:
        return {"ok": False, "reason": "rr_collapsed", "risk_reward": round(rr, 2)}

    return {
        "ok": True,
        "entry": float(spot),
        "stop_loss": round(new_stop, 2),
        "take_profits": [round(t, 2) for t in ahead],
        "risk_reward": round(rr, 2),
        "shifted": chase > 0,
        "drift_pct": round((spot - entry) / entry * 100.0, 2) if entry else 0.0,
        "dropped_tps": [round(t, 2) for t in tps if t not in ahead],
    }


def _trailed_stop(
    entry: float, ordered_tps: list[float], tps_hit: int
) -> float | None:
    """Mirror of the paper ladder's trail: after TP1 → breakeven, TP2 → TP1.

    Returns None while no target has filled, so an untouched trade keeps the
    structural stop the thesis chose.
    """
    if tps_hit <= 0:
        return None
    if tps_hit == 1:
        return float(entry)
    idx = min(tps_hit - 2, len(ordered_tps) - 1)
    return float(ordered_tps[idx]) if idx >= 0 else float(entry)


def _tps_taken(trade: dict[str, Any]) -> int:
    """How many ladder rungs price has actually reached.

    Counting ``reason == take_profit`` legs is wrong once a profitable stop
    (trailed to TP1) is mis-tagged — that looked like TP3 and the trail jumped
    a level. Prefix-count of rungs the fill prices have reached matches what
    the exchange did.
    """
    try:
        fills = json.loads(trade.get("exit_fills_json") or "{}")
    except json.JSONDecodeError:
        return 0
    side = str(trade.get("side") or "")
    entry = float(trade.get("entry") or 0)
    ordered = _ordered_tps(side, _as_tp_list(trade.get("take_profits_json")), entry)
    prices: list[float] = []
    reason_hits = 0
    for fill in fills.values():
        if not isinstance(fill, dict):
            continue
        if fill.get("reason") == "take_profit":
            reason_hits += 1
        if fill.get("reason") == "stop_loss":
            continue
        try:
            px = float(fill.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0:
            prices.append(px)
    if ordered and prices:
        count = 0
        for level in ordered:
            if any(_tp_reached(side, px, level) for px in prices):
                count += 1
            else:
                break
        return count
    return reason_hits


def _improves_stop(side: str, old: float | None, new: float) -> bool:
    """A trail may only ever reduce risk."""
    if old is None:
        return True
    return new > float(old) if side == "long" else new < float(old)


def retrail_exits(gw: Any, trade: dict[str, Any], new_stop: float) -> list[str]:
    """Move the stop on the resting brackets by cancel-and-replace.

    A bracket's stop leg can't be amended, so each rung is cancelled and
    re-placed individually rather than all at once — that way a single tranche
    is briefly uncovered instead of the whole position. Anything that fails to
    re-place is protected by a plain stop so no size is left naked.
    """
    instrument = trade["instrument"]
    side = str(trade["side"])
    closing = "sell" if side == "long" else "buy"
    csize = gw.contract_size(instrument)
    fresh: list[str] = []
    stranded = 0.0

    for oid in _exit_order_ids(trade):
        try:
            order = gw.get_order(oid)
        except GatewayError:
            logger.exception("Retrail: could not read %s", oid)
            fresh.append(oid)
            continue
        if str(order.get("status") or "") != "OPEN":
            continue
        cfg = (order.get("order_configuration") or {}).get("trigger_bracket_gtc") or {}
        contracts = float(cfg.get("base_size") or 0)
        limit_price = float(cfg.get("limit_price") or 0)
        if contracts <= 0 or limit_price <= 0:
            fresh.append(oid)
            continue

        leg_qty = contracts * csize
        try:
            gw.cancel_orders([oid])
        except GatewayError:
            logger.exception("Retrail: cancel failed for %s — leaving it in place", oid)
            fresh.append(oid)
            continue
        try:
            replaced = gw.place_bracket(
                instrument=instrument,
                side=closing,
                amount=leg_qty,
                limit_price=limit_price,
                stop_trigger_price=new_stop,
                label=f"{trade['source']}-retrail",
            )
            new_id = str(((replaced or {}).get("order") or {}).get("order_id") or "")
            if new_id:
                fresh.append(new_id)
            else:
                stranded += leg_qty
        except GatewayError:
            logger.exception(
                "Retrail: re-place failed for %.4f of %s", leg_qty, instrument
            )
            stranded += leg_qty

    if stranded > 0:
        logger.error(
            "Retrail left %.4f %s uncovered — placing a plain stop at %.2f",
            stranded, instrument, new_stop,
        )
        try:
            stop = gw.place_stop_market(
                instrument=instrument, side=closing, amount=stranded,
                trigger_price=new_stop, label=f"{trade['source']}-retrail-stop",
            )
            sid = str(((stop or {}).get("order") or {}).get("order_id") or "")
            if sid:
                fresh.append(sid)
        except GatewayError:
            logger.exception("Retrail fallback stop FAILED for %s", instrument)
            halt_live(
                f"retrail_uncovered:{instrument}:{stranded:.4f} — verify on Coinbase"
            )
    return fresh


def _maybe_trail_stop(gw: Any, trade_id: int, row: dict[str, Any]) -> None:
    """Reduce risk on the runner once a target has paid out."""
    tps_hit = _tps_taken(row)
    if tps_hit <= 0:
        return
    entry = float(row["entry"])
    ordered = _ordered_tps(
        str(row["side"]), _as_tp_list(row.get("take_profits_json")), entry
    )
    new_stop = _trailed_stop(entry, ordered, tps_hit)
    if new_stop is None or not _improves_stop(str(row["side"]), row.get("stop_loss"), new_stop):
        return
    logger.info(
        "Trailing stop on #%s after %d target(s): %s -> %.2f",
        trade_id, tps_hit, row.get("stop_loss"), new_stop,
    )
    fresh = retrail_exits(gw, row, new_stop)
    live_ledger.set_exit_orders(trade_id, fresh)
    live_ledger.set_stop_loss(trade_id, new_stop)
    if bot_config.LIVE_FILL_ALERTS_ENABLED:
        label = "breakeven" if tps_hit == 1 else f"TP{tps_hit - 1}"
        _notify_ops(
            f"STOP TRAILED #{trade_id} — {row.get('source')}\n"
            f"{row.get('product_id')} stop -> {new_stop:,.2f} ({label})\n"
            f"after {tps_hit} target(s) filled"
        )


def _as_tp_list(raw: Any) -> list[float]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    return [float(x) for x in parsed] if isinstance(parsed, list) else []


def _arm_plain_stop(
    gw: Any,
    instrument: str,
    closing: str,
    qty: float,
    stop_loss: float,
    label: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Single protective stop for the whole position — the fallback path."""
    stop = gw.place_stop_market(
        instrument=instrument,
        side=closing,
        amount=qty,
        trigger_price=float(stop_loss),
        label=f"{label}-stop",
    )
    stop_id = str(((stop or {}).get("order") or {}).get("order_id") or "")
    result["mode"] = "stop_only"
    result["stop_order_id"] = stop_id or None
    result["exit_order_ids"] = [stop_id] if stop_id else []
    return result


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
    """Always size mill clips to exactly one CDE nano contract.

    Dollar targeting used to request a fractional ETH size that the gateway
    then floored, so Telegram showed 0.104565 while the fill was 0.1000.
    One contract is the unit the venue actually fills. Sleeve capacity still
    rejects a contract whose notional no longer fits (BTC at a high print).
    """
    if price <= 0:
        return None
    floor = bot_config.LIVE_PRODUCT_QTY_FLOORS.get(product_id)
    if floor is None or floor <= 0:
        return None
    qty = round(float(floor), 6)
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
        "product_id": product_id,
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

    # Exits first, questions later: no protection → flatten + halt.
    try:
        exits = arm_exits(
            gw,
            instrument=instrument,
            side=side,
            qty=qty,
            entry=fill_price,
            stop_loss=float(suggestion.stop_loss),
            take_profits=suggestion.take_profits,
            label=f"{source}:{cycle_id or 'manual'}",
        )
    except GatewayError as exc:
        logger.error("EXITS REJECTED — flattening and halting live: %s", exc)
        try:
            gw.close_position(instrument)
        except GatewayError:
            logger.exception("Flatten after exit-reject ALSO failed — manual action required")
        halt_live(
            f"stop_reject:{instrument}:{exc} — position flattened, verify on Coinbase"
        )
        return None

    stop_order_id = exits.get("stop_order_id") or ""

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
        exit_order_ids=exits.get("exit_order_ids") or [],
    )

    # A target already through the market is banked during arming, so book it
    # against the row that was just written.
    direction = 1.0 if side == "long" else -1.0
    for leg in exits.get("realized") or []:
        live_ledger.record_partial_exit(
            trade_id,
            exit_qty=float(leg["qty"]),
            exit_price=float(leg["price"]),
            pnl_usd=(float(leg["price"]) - fill_price) * float(leg["qty"]) * direction,
            order_id=leg.get("order_id"),
            reason="take_profit",
        )

    # A gap-through target banks at arm time, which already earns the trail.
    if exits.get("realized"):
        try:
            row = live_ledger.get_trade(trade_id) or {}
            if float(row.get("qty_open") or 0) > 0:
                _maybe_trail_stop(gw, trade_id, row)
        except Exception:
            logger.exception("Stop trail after arming failed for #%s", trade_id)

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
        n_resting = len(exits.get("exit_order_ids") or [])
        if exits.get("mode") == "brackets":
            lines.append(f"{n_resting} target(s) resting on the exchange")
        elif exits.get("mode") == "stop_only":
            lines.append("NO take-profit orders — stop only, targets need manual arming")
        for leg in exits.get("realized") or []:
            lines.append(
                f"banked {leg['qty']:.4f} at {float(leg['price']):,.2f} "
                f"(target {float(leg['target']):,.2f} already through)"
            )
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


def _revalidated_plan(
    *,
    product_id: str,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profits: list[float],
) -> dict[str, Any]:
    """Fetch the live mark and re-check the plan against it.

    Fail-open on a mark we cannot read: the levels are then no worse than what
    the mill already vetted, and refusing every fill because the ticker is
    down would be its own outage.
    """
    if not bot_config.LIVE_REVALIDATE_ON_FILL:
        return {"ok": True, "entry": entry, "stop_loss": stop_loss,
                "take_profits": take_profits, "risk_reward": None, "skipped": True}
    spot = 0.0
    try:
        gw = get_gateway()
        instrument = gw.resolve_instrument(product_id)
        spot = float((gw.get_ticker(instrument) or {}).get("mark_price") or 0)
    except Exception:
        logger.exception("Revalidation could not read a mark for %s", product_id)
    if spot <= 0:
        # No mark to check against. The mill already vetted these levels, and
        # refusing every fill because the ticker is unreadable would be its own
        # outage, so the minted plan stands.
        logger.warning("Revalidation has no mark for %s — using minted levels", product_id)
        return {"ok": True, "entry": entry, "stop_loss": stop_loss,
                "take_profits": take_profits, "risk_reward": None, "no_mark": True}

    plan = revalidate_levels(
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=take_profits,
        spot=spot,
    )
    plan["spot"] = spot
    if plan.get("ok"):
        logger.info(
            "Revalidated %s %s at %.2f (was %.2f, %+.2f%%): R:R %.2f, targets %s",
            product_id, direction, spot, entry, plan["drift_pct"],
            plan["risk_reward"], plan["take_profits"],
        )
    else:
        logger.info(
            "Revalidation refused %s %s: %s (mark %.2f vs entry %.2f, stop %.2f)",
            product_id, direction, plan.get("reason"), spot, entry, stop_loss,
        )
    return plan


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

    # The idea was priced when it was minted; this fills at the price now.
    plan = _revalidated_plan(
        product_id=product_id,
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=list(take_profits or []),
    )
    if not plan.get("ok"):
        verdict["revalidation"] = plan
        return _skip(str(plan.get("reason") or "stale_levels"))
    verdict["revalidation"] = plan
    entry = float(plan["entry"])
    stop_loss = float(plan["stop_loss"])
    take_profits = list(plan["take_profits"])

    suggestion = Suggestion(
        action="deriv_buy" if direction == "long" else "deriv_sell",
        size=0.0,  # live sizing comes from the mill clip, not paper size
        entry=entry,
        stop_loss=stop_loss,
        take_profits=list(take_profits or []),
        risk_reward=plan.get("risk_reward"),
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
    """Book exit fills and close trades the exchange has released.

    Reads each trade's own resting exit orders rather than the instrument's net
    size. HQ and the mill can hold the same contract, so the netted position
    stays non-zero when one sleeve's exit fills, and a size check alone misses
    it — that let partial closes go unrecorded. Best-effort; never raises.
    """
    if config.EXECUTION_MODE != "live":
        return
    open_trades = live_ledger.get_open_trades()
    if not open_trades:
        return
    try:
        gw = get_gateway()
    except Exception:
        logger.exception("sync_live_positions: gateway unavailable")
        return

    for trade in open_trades:
        try:
            _reconcile_trade(gw, trade)
        except Exception:
            logger.exception("Reconcile failed for live trade #%s", trade.get("id"))

    for instrument in {t["instrument"] for t in open_trades}:
        try:
            _reconcile_flat_instrument(gw, instrument)
        except Exception:
            logger.exception("Flat-position check failed for %s", instrument)

    try:
        _sweep_orphan_exits(gw)
    except Exception:
        logger.exception("Orphan exit sweep failed")

    try:
        _check_daily_loss("hq")
        _check_daily_loss("mill")
    except Exception:
        logger.exception("Daily loss check after reconcile failed")


def _qty_open(trade: dict[str, Any]) -> float:
    """Remaining size. Pre-migration rows have no ``qty_open``; 0 is not None."""
    raw = trade.get("qty_open")
    return float(trade["qty"] if raw is None else raw)


def _reconcile_trade(gw: Any, trade: dict[str, Any]) -> None:
    """Book any exit legs of one trade that have filled since the last pass."""
    trade_id = int(trade["id"])
    entry = float(trade["entry"])
    direction = 1.0 if trade["side"] == "long" else -1.0
    csize = gw.contract_size(trade["instrument"])
    order_ids = _exit_order_ids(trade)
    if not order_ids:
        return

    exits: list[tuple[float, float, str]] = []
    for oid in order_ids:
        try:
            order = gw.get_order(oid)
        except GatewayError:
            logger.exception("Could not read exit order %s (trade #%s)", oid, trade_id)
            continue
        # Book only settled legs: a partially filled bracket would otherwise be
        # booked now and again later at a different average price.
        if str(order.get("status") or "") not in ("FILLED", "EXPIRED", "CANCELLED"):
            continue
        qty = float(order.get("filled_size") or 0.0) * csize
        price = float(order.get("average_filled_price") or 0.0)
        if qty <= 0 or price <= 0:
            continue
        pnl = (price - entry) * qty * direction
        reason = _classify_fill_reason(
            order,
            side=str(trade["side"]),
            entry=entry,
            price=price,
            take_profits=_as_tp_list(trade.get("take_profits_json")),
            stop_loss=trade.get("stop_loss"),
        )
        if live_ledger.record_partial_exit(
            trade_id,
            exit_qty=qty,
            exit_price=price,
            pnl_usd=pnl,
            order_id=oid,
            reason=reason,
        ):
            exits.append((qty, price, reason))
            logger.info(
                "Live trade #%s exit leg booked: %.4f @ %.2f (%s, pnl %.2f)",
                trade_id, qty, price, reason, pnl,
            )

    if not exits:
        # Nothing new filled, but the stop can still be behind where the trail
        # says it belongs — a target booked before the trail existed, or an
        # earlier re-place that failed. Checked every pass so it self-heals.
        try:
            _maybe_trail_stop(gw, trade_id, trade)
        except Exception:
            logger.exception("Stop trail failed for live trade #%s", trade_id)
        return

    row = live_ledger.get_trade(trade_id) or {}
    qty_open = float(row.get("qty_open") or 0.0)
    if qty_open > csize * 0.5:
        if bot_config.LIVE_FILL_ALERTS_ENABLED:
            banked = float(row.get("realized_pnl_usd") or 0.0)
            _notify_ops(
                f"LIVE PARTIAL #{trade_id} — {trade['source']}\n"
                + "\n".join(
                    f"{reason} {qty:.4f} @ {price:,.2f}" for qty, price, reason in exits
                )
                + f"\nbanked {banked:+,.2f} · {qty_open:.4f} still open"
            )
        try:
            _maybe_trail_stop(gw, trade_id, row)
        except Exception:
            logger.exception("Stop trail failed for live trade #%s", trade_id)
        return

    _close_out(gw, trade_id, row, reason=exits[-1][2])


def _close_out(
    gw: Any, trade_id: int, row: dict[str, Any], *, reason: str
) -> None:
    """Finalise a fully-exited trade and cancel whatever is still resting."""
    fills = json.loads(row.get("exit_fills_json") or "{}")
    total_qty = sum(float(f.get("qty") or 0) for f in fills.values())
    avg_exit = (
        sum(float(f["qty"]) * float(f["price"]) for f in fills.values()) / total_qty
        if total_qty
        else float(row.get("entry") or 0)
    )
    live_ledger.record_close(
        trade_id, exit_price=avg_exit, pnl_usd=0.0, close_reason=reason
    )
    _cancel_exit_orders(gw, row)
    total = float(row.get("realized_pnl_usd") or 0.0)
    logger.info(
        "Live trade #%s closed (%s) avg exit %.2f, pnl %.2f",
        trade_id, reason, avg_exit, total,
    )
    if bot_config.LIVE_FILL_ALERTS_ENABLED:
        _notify_ops(
            f"LIVE CLOSE #{trade_id} — {row.get('source')}\n"
            f"{row.get('product_id')} {row.get('side')} {float(row.get('qty') or 0):.4f}\n"
            f"entry {float(row.get('entry') or 0):,.2f} -> avg exit {avg_exit:,.2f}\n"
            f"P&L {total:+,.2f} ({reason})"
        )
    if str(row.get("source") or "") == "mill":
        _refill_mill_sleeve()
    elif str(row.get("source") or "") == "hq":
        try:
            import case_study

            case_study.queue_generate(trade_id)
        except Exception:
            logger.exception("Case study queue failed for live trade #%s", trade_id)


def _refill_mill_sleeve() -> None:
    """A closed clip frees the sleeve, so look for something to put in it."""
    try:
        import trade_ideas_bridge

        trade_ideas_bridge.sweep_reoffer()
    except Exception:
        logger.exception("Re-offer sweep failed after a mill clip closed")


def _reconcile_flat_instrument(gw: Any, instrument: str) -> None:
    """Catch closes the exit orders can't explain, e.g. a manual flatten.

    Only acts when the exchange holds nothing: with two sleeves netted into one
    contract, a partial shortfall can't be attributed to a specific trade, so
    that case is logged for a human rather than guessed at.
    """
    open_trades = [
        t for t in live_ledger.get_open_trades() if t["instrument"] == instrument
    ]
    if not open_trades:
        return
    pos = gw.get_position(instrument)
    size = abs(float((pos or {}).get("size") or 0.0))
    ledger_qty = sum(_qty_open(t) for t in open_trades)
    if size > 0:
        if ledger_qty - size > gw.contract_size(instrument) * 0.5:
            logger.warning(
                "%s: exchange holds %.4f but ledger expects %.4f across %d trades "
                "— unattributed shortfall, needs review",
                instrument, size, ledger_qty, len(open_trades),
            )
        return

    mark = float((pos or {}).get("mark_price") or 0.0)
    for trade in open_trades:
        trade_id = int(trade["id"])
        qty_open = _qty_open(trade)
        exit_price = mark or float(trade.get("stop_loss") or trade["entry"])
        direction = 1.0 if trade["side"] == "long" else -1.0
        pnl = (exit_price - float(trade["entry"])) * qty_open * direction
        live_ledger.record_partial_exit(
            trade_id,
            exit_qty=qty_open,
            exit_price=exit_price,
            pnl_usd=pnl,
            order_id=f"flat:{trade_id}",
            reason="exchange_close",
        )
        row = live_ledger.get_trade(trade_id) or {}
        _close_out(gw, trade_id, row, reason="exchange_close")


def _exit_order_ids(trade: dict[str, Any]) -> list[str]:
    try:
        ids = json.loads(trade.get("exit_order_ids_json") or "[]")
    except json.JSONDecodeError:
        ids = []
    if not ids and trade.get("stop_order_id"):
        ids = [str(trade["stop_order_id"])]
    return [str(i) for i in ids if i]


def _cancel_exit_orders(gw: Any, trade: dict[str, Any]) -> None:
    ids = _exit_order_ids(trade)
    if not ids:
        return
    try:
        gw.cancel_orders(ids)
    except GatewayError:
        logger.exception("Could not cancel exit orders %s", ids)


def _sweep_orphan_exits(gw: Any) -> None:
    """Cancel resting exits no open trade owns.

    The venue rejects ``reduce_only``, so an exit left behind after its
    position is gone can open a brand-new position in the opposite direction.
    Brackets are sized against the live position and so are largely self-
    limiting, but the plain-stop fallback is not.
    """
    open_trades = live_ledger.get_open_trades()
    owned = {oid for t in open_trades for oid in _exit_order_ids(t)}
    for instrument in {t["instrument"] for t in open_trades} | _recent_instruments():
        try:
            resting = gw.get_open_orders(instrument)
        except GatewayError:
            logger.exception("Could not list open orders for %s", instrument)
            continue
        orphans = [
            str(o.get("order_id"))
            for o in resting
            if str(o.get("order_id")) not in owned
        ]
        if not orphans:
            continue
        logger.warning(
            "%s: cancelling %d orphaned exit order(s) %s",
            instrument, len(orphans), orphans,
        )
        try:
            gw.cancel_orders(orphans)
        except GatewayError:
            logger.exception("Orphan cancel failed for %s", instrument)


def _recent_instruments() -> set[str]:
    """Instruments from recently closed trades, so their leftovers get swept."""
    try:
        return {
            str(t["instrument"])
            for t in live_ledger.get_closed_trades(limit=10)
            if t.get("instrument")
        }
    except Exception:
        logger.exception("Recent instrument lookup failed")
        return set()


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
