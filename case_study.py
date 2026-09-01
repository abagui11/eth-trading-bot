"""Eva trade case-study charts — annotated close graphics for HQ live fills.

Generated when an Eva (HQ) trade fully closes. Mill clips are out of scope.
The numeric skeleton (prices, times, R-multiples, size closed) is always
taken from the live ledger. The original entry rationale is a required input
to copy generation — it is read first, then the outcome facts — so the
callouts retell the thesis rather than inventing a new one. Claude writes
the qualitative sentences that sit under each numbered title, matching a
fixed role script:

  1. Entry and what set it
  2. Stop and what set it
  3–N. Take-profits that actually paid (last remaining close is FULL EXIT)
  Last. Discretionary post-trade note (the "already flat / price rebounded" box)

Failure is silent: a close must never wait on OHLC, the LLM, or matplotlib.
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
from matplotlib.offsetbox import AnnotationBbox, TextArea, VPacker

import bot_config
import config
import live_ledger
import research

logger = logging.getLogger(__name__)

_KIND_COLORS = {
    "entry": "#e2b714",
    "stop": "#f85149",
    "tp": "#3fb950",
    "exit": "#3fb950",
    "stopped": "#f85149",
    "misc": "#58a6ff",
}
_BOX_FACE = "#1b222d"
_FIG_BG = "#131722"
_GRID = "#1e222d"
_MUTED = "#787b86"
_TEXT = "#d1d4dc"
_FOOTER = "#e2b714"

_FIGSIZE = (16.0, 9.0)
_DPI = 144
_BODY_WRAP = 40

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_INFLIGHT: set[int] = set()
_INFLIGHT_LOCK = threading.Lock()

_RATIONALE_MAX = 4000

CASE_STUDY_SYSTEM = """You write the qualitative sentences on Eva trade case-study charts.

You receive two inputs, in this order:

1. ENTRY RATIONALE — the original audited thesis for taking the trade. This is
   the source of truth for *why* we entered and *where* the stop was. Entry and
   stop sentences MUST condense this rationale. Do not replace it with a
   generic "order-block entry" if the rationale names a more specific setup
   (SFP, breaker, fib, HTF bias, liquidity). Do not add setups, levels, or
   catalysts that are not in the rationale or in FACTS. If the rationale is
   empty, fall back to entry_set_by / stop_set_by in FACTS.

2. FACTS — prices, times, R-multiples, size closed, and post-exit candle
   stats measured from the live ledger and Coinbase OHLC. You do not invent,
   round, or replace any number. Titles, prices, R-multiples, timestamps, and
   the aggregate-return footer are rendered by code from those facts. You
   only write the short "why" sentences under each title.

Return JSON only, no markdown, no commentary, of the form:
{
  "bodies": {
    "1": "one or two sentences",
    "2": "one or two sentences"
  },
  "misc_title": "SHORT ALL-CAPS HEADLINE — CLAUSE",
  "misc_body": "one or two sentences about what price did after the book was already flat"
}

Roles you will see (ids are 1-based, sequential, and already assigned):
- kind=entry: condense the ENTRY RATIONALE into why the fill happened at this
  price. You may mention the trigger time from FACTS. Do not claim a top or
  bottom you cannot see in FACTS.
- kind=stop: condense from the rationale what the stop was anchored to
  (order block, swept swing, plan). If stop_touched is false, say price never
  came back to it. If the stop later trailed, you may mention that in passing;
  the printed level is the opening stop.
- kind=tp: a partial that paid at a planned target. Only these slots paid.
  take_profits in FACTS is the plan — do not claim a later target paid
  unless it has a kind=tp slot of its own.
- kind=exit: the last remaining size closed at a target. This is the
  "full exit" box, not an extra take-profit.
- kind=stopped: remaining size hit the (possibly trailed) stop. Treat it
  as a stop-out even if the fill was still profitable versus entry.
- kind=misc: ALWAYS the last id. This is the discretionary callout — the
  point of running an LLM. Look at post_exit. Typical patterns:
    * price continued then reversed, and the book was already flat
    * price never looked back
    * a stop-out was immediately followed by the move that would have paid
  misc_title is 3–8 words, all caps, like "PRICE REBOUNDED — FLAT ALREADY"
  or "CONTINUED THROUGH — ALREADY OUT". No prices in the title (the body
  may repeat the post_exit high/low/times that were given to you).

Voice:
- Present tense or simple past. No hype, no "perfect", no "guaranteed".
- No financial advice. No "should have".
- American English. No markdown. No emoji.
- Each body is at most 220 characters.
- You MAY quote prices and times that appear in FACTS. You may NOT invent
  any other number, percentage, or clock time.
- Do not mention TradingView, Eva's model name, or that you are an LLM.
"""


@dataclass
class Slot:
    n: int
    kind: str
    title: str
    body: str
    price: float
    ts: str | None
    place: str
    footer: str | None = None
    color: str = field(init=False)

    def __post_init__(self) -> None:
        self.color = _KIND_COLORS.get(self.kind, _TEXT)


def _parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    raw = str(ts).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_px(price: float) -> str:
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 100:
        return f"{price:,.2f}"
    return f"{price:.4f}"


def _fmt_when(ts: str | None) -> str:
    dt = _parse_utc(ts)
    if dt is None:
        return ""
    return dt.strftime("%H:%M")


def _fmt_date(ts: str | None) -> str:
    dt = _parse_utc(ts)
    if dt is None:
        return ""
    return dt.strftime("%d %b %Y")


def _r_multiple(entry: float, stop: float | None, price: float) -> float | None:
    if stop is None:
        return None
    risk = abs(float(entry) - float(stop))
    if risk <= 1e-9:
        return None
    return abs(float(price) - float(entry)) / risk


def _r_label(r: float | None) -> str:
    if r is None:
        return ""
    return f" ({r:.1f}R)"


def pick_granularity(opened_at: str | None, closed_at: str | None) -> tuple[str, str]:
    """Choose a candle size so the open→close window is readable, not sparse."""
    start = _parse_utc(opened_at)
    end = _parse_utc(closed_at) or datetime.now(timezone.utc)
    if start is None:
        return "FIVE_MINUTE", "M5"
    seconds = max(0.0, (end - start).total_seconds())
    if seconds <= 3 * 3600:
        return "ONE_MINUTE", "M1"
    if seconds <= 18 * 3600:
        return "FIVE_MINUTE", "M5"
    return "ONE_HOUR", "H1"


def _entry_set_by(tags: list[str], order_block: dict | None, fill_type: str) -> str:
    if str(fill_type or "").lower() == "manual":
        return "Operator fill at the plan's entry"
    joined = " ".join(t.lower() for t in tags)
    ob_bit = ""
    if isinstance(order_block, dict):
        try:
            lo = float(order_block.get("low"))
            hi = float(order_block.get("high"))
            ob_bit = f" ({_fmt_px(lo)}–{_fmt_px(hi)})"
        except (TypeError, ValueError):
            ob_bit = ""
    if "sfp" in joined:
        return "Swing-failure / liquidity-sweep reversal"
    if "ob" in joined or "order_block" in joined or "order-block" in joined:
        return f"Order-block entry in the 0.25–0.50 fib band{ob_bit}"
    if "watchdog" in joined:
        return "Watchdog trigger at the plan's entry"
    if tags:
        return "Audited ICT plan (" + ", ".join(tags[:3]) + ")"
    return "Audited ICT plan"


def _stop_set_by(
    tags: list[str],
    order_block: dict | None,
    side: str,
    initial_stop: float | None,
) -> str:
    joined = " ".join(t.lower() for t in tags)
    if isinstance(order_block, dict):
        try:
            lo = float(order_block.get("low"))
            hi = float(order_block.get("high"))
        except (TypeError, ValueError):
            lo = hi = None
        else:
            if side == "short":
                return f"Just beyond the originating order-block high ({_fmt_px(hi)})"
            return f"Just beyond the originating order-block low ({_fmt_px(lo)})"
    if "sfp" in joined:
        return "Beyond the swept swing that triggered the entry"
    if initial_stop is not None:
        return "Structural stop from the audited plan"
    return "Protective stop from the audited plan"


def _story(cycle_id: str | None) -> dict[str, Any]:
    if not cycle_id:
        return {}
    try:
        from dashboard.data import _trade_story_from_cycle

        return _trade_story_from_cycle(cycle_id) or {}
    except Exception:
        logger.exception("Case study story lookup failed for cycle %s", cycle_id)
        return {}


def _thesis(text: str) -> str:
    """Keep the entry thesis; drop the appended Market context block."""
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        from critic import split_rationale

        thesis, _ctx = split_rationale(raw)
        chosen = (thesis or raw).strip()
    except Exception:
        chosen = raw
    if len(chosen) > _RATIONALE_MAX:
        return chosen[:_RATIONALE_MAX].rsplit(" ", 1)[0] + "…"
    return chosen


def _excerpt(text: str, limit: int = 180) -> str:
    """First sentence of the thesis, for deterministic entry copy."""
    compact = " ".join((text or "").split())
    if not compact:
        return ""
    for sep in (". ", "! ", "? "):
        idx = compact.find(sep)
        if 24 <= idx <= limit:
            return compact[: idx + 1]
    if len(compact) <= limit:
        return compact
    return compact[:limit].rsplit(" ", 1)[0] + "…"


def _as_float_list(raw: Any) -> list[float]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, (int, float)):
        return [float(raw)]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[float] = []
    for item in raw:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def _legs(row: dict[str, Any]) -> list[dict[str, Any]]:
    from dashboard.data import exit_legs

    return exit_legs(row.get("exit_fills_json"))


def _price_reached_tp(side: str, price: float, level: float) -> bool:
    return price >= level if side == "long" else price <= level


def _paid_tp_rungs(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Scale-outs that actually matched a planned target, excluding the last close.

    The last remaining fill is the exit/stop box — even when it tagged
    ``take_profit`` because a trailed stop was still green versus entry.
    """
    final_ats = {
        str(leg.get("at") or "")
        for leg in facts.get("legs") or []
        if leg.get("is_final")
    }
    paid: list[dict[str, Any]] = []
    for rung in facts.get("tp_progress") or []:
        if not rung.get("hit") or rung.get("pnl_usd") is None:
            continue
        if str(rung.get("at") or "") in final_ats:
            continue
        paid.append(rung)
    return paid


def build_facts(
    row: dict[str, Any],
    *,
    story: dict[str, Any] | None = None,
    rationale: str | None = None,
) -> dict[str, Any]:
    """Numeric skeleton plus the entry thesis. Safe to feed to the LLM.

    ``rationale`` is the original reason for taking the trade. Pass it in when
    the caller already has it; otherwise it is loaded from the originating
    cycle. The Market context appendix is stripped so copy generation sees
    the thesis, not the pulse.
    """
    story = story if story is not None else _story(row.get("cycle_id"))
    source_rationale = (
        rationale if rationale is not None else str(story.get("rationale") or "")
    )
    thesis = _thesis(source_rationale)
    side = str(row.get("side") or "long")
    entry = float(row.get("entry") or 0)
    qty = float(row.get("qty") or 0)
    product_id = str(row.get("product_id") or story.get("product_id") or "ETH-USD")
    tags = [str(t) for t in (story.get("setup_tags") or []) if t]
    order_block = story.get("order_block")
    if not isinstance(order_block, dict):
        order_block = None
    from dashboard.data import build_tp_progress, ordered_take_profits, recover_opening_stop

    raw_initial = row.get("initial_stop_loss")
    initial_stop = recover_opening_stop(
        side,
        entry,
        float(raw_initial) if raw_initial is not None else None,
        story.get("stop_loss"),
    )
    if initial_stop is None and row.get("stop_loss") is not None:
        initial_stop = float(row["stop_loss"])
    current_stop = float(row["stop_loss"]) if row.get("stop_loss") is not None else None
    tps = _as_float_list(row.get("take_profits_json")) or _as_float_list(
        story.get("take_profits")
    )
    legs = _legs(row)
    tp_progress = build_tp_progress(side, entry, tps, legs=legs)
    ordered_tps = ordered_take_profits(side, tps, entry)
    notional = entry * qty if entry and qty else 0.0
    pnl = float(row.get("pnl_usd") or 0.0)
    pnl_pct = (pnl / notional * 100.0) if notional else 0.0
    exit_price = float(row.get("exit_price") or 0) or None
    close_reason = str(row.get("close_reason") or "")
    fill_type = str(row.get("fill_type") or "auto")

    leg_facts: list[dict[str, Any]] = []
    closed_qty = 0.0
    for leg in legs:
        closed_qty += float(leg.get("qty") or 0)
        price = float(leg.get("price") or 0)
        fraction = (float(leg.get("qty") or 0) / qty) if qty else 0.0
        remaining_after = max(qty - closed_qty, 0.0)
        leg_facts.append(
            {
                "reason": str(leg.get("reason") or ""),
                "price": price,
                "qty": float(leg.get("qty") or 0),
                "pnl_usd": float(leg.get("pnl_usd") or 0),
                "at": str(leg.get("at") or ""),
                "r_multiple": _r_multiple(entry, initial_stop, price),
                "fraction_closed": fraction,
                "remaining_after": remaining_after,
                "is_final": remaining_after <= 1e-9,
            }
        )
    if not leg_facts and exit_price:
        # Flattened without per-leg JSON — treat as a single full exit.
        leg_facts.append(
            {
                "reason": close_reason or "exit",
                "price": exit_price,
                "qty": qty,
                "pnl_usd": pnl,
                "at": str(row.get("closed_at") or ""),
                "r_multiple": _r_multiple(entry, initial_stop, exit_price),
                "fraction_closed": 1.0,
                "remaining_after": 0.0,
                "is_final": True,
            }
        )

    # A profitable trailed stop is stored as take_profit. If the last print
    # never reached a planned target, the chart must not call it TP3.
    for leg in reversed(leg_facts):
        if not leg.get("is_final"):
            continue
        reached = any(
            _price_reached_tp(side, float(leg["price"]), level)
            for level in ordered_tps
        )
        if str(leg.get("reason") or "") == "stop_loss" or not reached:
            leg["reason"] = "stop_loss"
            close_reason = "stop_loss"
        break

    return {
        "trade_id": int(row["id"]),
        "source": str(row.get("source") or ""),
        "status": str(row.get("status") or ""),
        "product_id": product_id,
        "product_label": bot_config.product_label(product_id),
        "instrument": str(row.get("instrument") or ""),
        "side": side,
        "qty": qty,
        "notional_usd": notional,
        "entry": entry,
        "opened_at": str(row.get("opened_at") or ""),
        "closed_at": str(row.get("closed_at") or ""),
        "exit_price": exit_price,
        "pnl_usd": pnl,
        "pnl_pct": pnl_pct,
        "pnl_per_million": (pnl / notional * 1_000_000.0) if notional else 0.0,
        "close_reason": close_reason,
        "fill_type": fill_type,
        "setup_tags": tags,
        "rationale": thesis,
        "rationale_excerpt": _excerpt(thesis),
        "order_block": (
            {"low": order_block.get("low"), "high": order_block.get("high")}
            if order_block
            else None
        ),
        "entry_set_by": _entry_set_by(tags, order_block, fill_type),
        "initial_stop": initial_stop,
        "current_stop": current_stop,
        "stop_trailed": (
            initial_stop is not None
            and current_stop is not None
            and abs(current_stop - initial_stop) > 1e-6
        ),
        "stop_set_by": _stop_set_by(tags, order_block, side, initial_stop),
        "take_profits": tps,
        "tp_progress": tp_progress,
        "legs": leg_facts,
        "risk_usd": (
            abs(entry - initial_stop) if initial_stop is not None else None
        ),
    }


def _stop_touched(facts: dict[str, Any], bars: list[dict]) -> bool:
    stop = facts.get("initial_stop")
    if stop is None or not bars:
        return False
    side = facts["side"]
    opened = _parse_utc(facts.get("opened_at"))
    closed = _parse_utc(facts.get("closed_at"))
    for bar in bars:
        ts = _parse_utc(str(bar.get("ts") or ""))
        if ts is None:
            continue
        if opened and ts < opened:
            continue
        if closed and ts > closed:
            break
        high = float(bar.get("high") or 0)
        low = float(bar.get("low") or 0)
        if side == "short" and high >= float(stop):
            return True
        if side == "long" and low <= float(stop):
            return True
    return False


def _post_exit_stats(facts: dict[str, Any], bars: list[dict]) -> dict[str, Any]:
    closed = _parse_utc(facts.get("closed_at"))
    after: list[dict] = []
    for bar in bars:
        ts = _parse_utc(str(bar.get("ts") or ""))
        if ts is None or closed is None or ts <= closed:
            continue
        after.append(bar)
    if not after:
        return {}
    lows = [(float(b["low"]), str(b["ts"])) for b in after]
    highs = [(float(b["high"]), str(b["ts"])) for b in after]
    lo, lo_at = min(lows, key=lambda p: p[0])
    hi, hi_at = max(highs, key=lambda p: p[0])
    last = float(after[-1]["close"])
    exit_px = float(facts.get("exit_price") or last)
    extreme = lo if facts["side"] == "short" else hi
    rebound = abs(last - extreme)
    rebound_pct = (rebound / extreme * 100.0) if extreme else 0.0
    against = (last - exit_px) if facts["side"] == "short" else (exit_px - last)
    return {
        "bars_after": len(after),
        "low": lo,
        "low_at": lo_at,
        "high": hi,
        "high_at": hi_at,
        "last": last,
        "rebound_pct": rebound_pct,
        "moved_against_after_exit": against > 0,
        "against_usd": against,
    }


def _aggregate_footer(facts: dict[str, Any]) -> str:
    pnl = float(facts.get("pnl_usd") or 0)
    pct = float(facts.get("pnl_pct") or 0)
    per_m = float(facts.get("pnl_per_million") or 0)
    return (
        f"Aggregate Return: {pct:+.1f}%  ·  "
        f"P&L: ${pnl:+,.0f}  ·  per $1m: ${per_m:+,.0f}"
    )


def build_slots(facts: dict[str, Any], *, copy: dict[str, Any] | None = None) -> list[Slot]:
    """Fixed role order. `copy` supplies LLM/fallback bodies keyed by id."""
    copy = copy or {}
    bodies: dict[str, str] = dict(copy.get("bodies") or {})
    side = str(facts["side"]).upper()
    entry = float(facts["entry"])
    slots: list[Slot] = []

    def body_for(n: int, fallback: str) -> str:
        text = str(bodies.get(str(n)) or bodies.get(n) or "").strip()
        return text or fallback

    n = 1
    slots.append(
        Slot(
            n=n,
            kind="entry",
            title=f"{n} {side} ENTRY — {_fmt_px(entry)}",
            body=body_for(n, facts["entry_set_by"] + "."),
            price=entry,
            ts=facts.get("opened_at"),
            place="tr",
        )
    )
    n += 1

    initial_stop = facts.get("initial_stop")
    if initial_stop is not None:
        stop_fb = facts["stop_set_by"] + "."
        if facts.get("stop_touched") is False:
            stop_fb = facts["stop_set_by"] + " — price never came back to touch it."
        slots.append(
            Slot(
                n=n,
                kind="stop",
                title=f"{n} STOP LOSS — {_fmt_px(float(initial_stop))}",
                body=body_for(n, stop_fb),
                price=float(initial_stop),
                ts=facts.get("opened_at"),
                place="tc",
            )
        )
        n += 1

    paid = _paid_tp_rungs(facts)
    final_legs = [leg for leg in facts.get("legs") or [] if leg.get("is_final")]
    # A single full close is the exit box, not a TP box.
    for rung in paid:
        r = _r_label(_r_multiple(entry, initial_stop, float(rung["price"])))
        frac = (float(rung.get("qty") or 0) / float(facts.get("qty") or 1)) if facts.get("qty") else 0.0
        fallback = (
            f"Partial take-profit, {frac:.0%} of the starting position closed."
        )
        slots.append(
            Slot(
                n=n,
                kind="tp",
                title=f"{n} {rung['label']} — {_fmt_px(float(rung['price']))}{r}",
                body=body_for(n, fallback),
                price=float(rung["price"]),
                ts=rung.get("at") or None,
                place="left",
            )
        )
        n += 1

    for leg in final_legs:
        reason = str(leg.get("reason") or facts.get("close_reason") or "")
        stopped = reason == "stop_loss"
        r = _r_label(leg.get("r_multiple"))
        kind = "stopped" if stopped else "exit"
        label = "STOPPED OUT" if stopped else "FULL EXIT"
        fallback = (
            "Protective stop filled — remaining size closed."
            if stopped
            else "Remaining size closed. Book is flat."
        )
        slots.append(
            Slot(
                n=n,
                kind=kind,
                title=f"{n} {label} — {_fmt_px(float(leg['price']))}{r}",
                body=body_for(n, fallback),
                price=float(leg["price"]),
                ts=leg.get("at") or facts.get("closed_at"),
                place="bl",
                footer=_aggregate_footer(facts),
            )
        )
        n += 1

    post = facts.get("post_exit") or {}
    misc_title = str(copy.get("misc_title") or "").strip() or _fallback_misc_title(facts)
    misc_body = str(copy.get("misc_body") or bodies.get(str(n)) or "").strip()
    if not misc_body:
        misc_body = _fallback_misc_body(facts)
    misc_price = float(
        post.get("low")
        if facts["side"] == "short" and post.get("low") is not None
        else post.get("high")
        if post.get("high") is not None
        else facts.get("exit_price")
        or facts["entry"]
    )
    misc_ts = (
        post.get("low_at")
        if facts["side"] == "short"
        else post.get("high_at")
    ) or facts.get("closed_at")
    slots.append(
        Slot(
            n=n,
            kind="misc",
            title=f"{n} {misc_title}",
            body=misc_body,
            price=misc_price,
            ts=misc_ts,
            place="br",
        )
    )
    return slots


def _fallback_misc_title(facts: dict[str, Any]) -> str:
    post = facts.get("post_exit") or {}
    if post.get("moved_against_after_exit"):
        return "PRICE REBOUNDED — FLAT ALREADY"
    if post:
        return "FOLLOW-THROUGH — ALREADY FLAT"
    return "BOOK IS FLAT"


def _fallback_misc_body(facts: dict[str, Any]) -> str:
    post = facts.get("post_exit") or {}
    if not post:
        return "Chart captured at close — the position was already flat."
    lo = post.get("low")
    hi = post.get("high")
    lo_at = _fmt_when(post.get("low_at"))
    hi_at = _fmt_when(post.get("high_at"))
    if facts["side"] == "short" and lo is not None and hi is not None:
        return (
            f"Low printed {_fmt_px(float(lo))}"
            + (f" at {lo_at}" if lo_at else "")
            + f", then traded up to {_fmt_px(float(hi))}"
            + (f" by {hi_at}" if hi_at else "")
            + " — the trade was already closed."
        )
    if lo is not None and hi is not None:
        return (
            f"High printed {_fmt_px(float(hi))}"
            + (f" at {hi_at}" if hi_at else "")
            + f", then traded down to {_fmt_px(float(lo))}"
            + (f" by {lo_at}" if lo_at else "")
            + " — the trade was already closed."
        )
    return "Price kept moving after the book was already flat."


def fallback_copy(facts: dict[str, Any]) -> dict[str, Any]:
    """Deterministic sentences when the LLM is off or fails."""
    bodies: dict[str, str] = {}
    n = 1
    when = _fmt_when(facts.get("opened_at"))
    excerpt = str(facts.get("rationale_excerpt") or "").strip()
    trigger = facts["entry_set_by"]
    if excerpt:
        lead = f"Triggered {when} — " if when else ""
        bodies[str(n)] = lead + excerpt
    else:
        bodies[str(n)] = (
            (f"Triggered {when} — " if when else "Triggered — ")
            + trigger[0].lower()
            + trigger[1:]
            + "."
        )
    n += 1
    if facts.get("initial_stop") is not None:
        extra = ""
        if facts.get("stop_touched") is False:
            extra = " Price never came back to touch it."
        elif facts.get("stop_trailed"):
            extra = (
                f" Later trailed to {_fmt_px(float(facts['current_stop']))}."
            )
        bodies[str(n)] = facts["stop_set_by"] + "." + extra
        n += 1
    paid = _paid_tp_rungs(facts)
    final_legs = [leg for leg in facts.get("legs") or [] if leg.get("is_final")]
    for rung in paid:
        frac = (float(rung.get("qty") or 0) / float(facts.get("qty") or 1)) if facts.get("qty") else 0.0
        r = _r_multiple(
            float(facts["entry"]),
            facts.get("initial_stop"),
            float(rung["price"]),
        )
        r_bit = f", locking in {r:.1f}R" if r else ""
        bodies[str(n)] = (
            f"{rung['label']}{r_bit}. {frac:.0%} of the starting position closed."
        )
        n += 1
    for leg in final_legs:
        when_x = _fmt_when(leg.get("at") or facts.get("closed_at"))
        if str(leg.get("reason") or "") == "stop_loss":
            bodies[str(n)] = (
                "Protective stop filled"
                + (f" at {when_x}" if when_x else "")
                + " — remaining size closed."
            )
        else:
            bodies[str(n)] = (
                "100% closed"
                + (f" at {when_x}" if when_x else "")
                + " — remaining size off the book."
            )
        n += 1
    return {
        "bodies": bodies,
        "misc_title": _fallback_misc_title(facts),
        "misc_body": _fallback_misc_body(facts),
    }


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    text = _JSON_FENCE.sub("", (raw or "").strip()).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    bodies = data.get("bodies")
    if bodies is not None and not isinstance(bodies, dict):
        return None
    out: dict[str, Any] = {"bodies": {}}
    for key, val in (bodies or {}).items():
        text_val = str(val or "").strip()
        if text_val:
            out["bodies"][str(key)] = text_val[:220]
    title = str(data.get("misc_title") or "").strip()
    if title:
        out["misc_title"] = title[:80].upper()
    body = str(data.get("misc_body") or "").strip()
    if body:
        out["misc_body"] = body[:280]
    return out


def llm_copy(facts: dict[str, Any]) -> dict[str, Any] | None:
    if not bot_config.USE_LLM_CASE_STUDY:
        return None
    payload = {
        k: facts[k]
        for k in (
            "product_id",
            "side",
            "opened_at",
            "closed_at",
            "entry",
            "entry_set_by",
            "initial_stop",
            "stop_set_by",
            "stop_touched",
            "stop_trailed",
            "current_stop",
            "qty",
            "notional_usd",
            "pnl_usd",
            "pnl_pct",
            "close_reason",
            "setup_tags",
            "order_block",
            "legs",
            "take_profits",
            "tp_progress",
            "post_exit",
        )
        if k in facts
    }
    rationale = str(facts.get("rationale") or "").strip()
    # The model only needs to know the id/kind/title of each slot so its
    # bodies dict keys match. Bodies themselves are blank on purpose.
    skeleton = []
    for slot in build_slots(facts, copy={"bodies": {}, "misc_title": "", "misc_body": ""}):
        skeleton.append(
            {
                "id": slot.n,
                "kind": slot.kind,
                "title": slot.title,
            }
        )
    payload["slots"] = skeleton
    try:
        import anthropic
        from analyze import log_anthropic_usage

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        user = (
            "ENTRY RATIONALE (read this first — it is the original thesis for "
            "taking the trade. Entry and stop copy must condense this, not "
            "replace it):\n\n"
            + (rationale or "(no rationale stored for this cycle)")
            + "\n\nFACTS (ledger + candles — numbers only from here):\n"
            + json.dumps(payload, default=str)
        )
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL_FAST,
            max_tokens=700,
            system=CASE_STUDY_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        log_anthropic_usage(response, "case_study")
    except Exception:
        logger.exception("Case-study LLM call failed for trade #%s", facts.get("trade_id"))
        return None
    raw = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            raw += block.text
    parsed = _parse_llm_json(raw)
    if not parsed:
        logger.warning("Case-study LLM returned unusable JSON for trade #%s", facts.get("trade_id"))
    return parsed


def fetch_bars(facts: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    gran, label = pick_granularity(facts.get("opened_at"), facts.get("closed_at"))
    start = _parse_utc(facts.get("opened_at"))
    end = _parse_utc(facts.get("closed_at")) or datetime.now(timezone.utc)
    if start is None:
        raise ValueError("opened_at missing")
    seconds = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "ONE_HOUR": 3600}[gran]
    pad_before = 12 * seconds
    pad_after = 8 * seconds
    now_ts = int(time.time())
    start_ts = int(start.timestamp()) - pad_before
    end_ts = min(int(end.timestamp()) + pad_after, now_ts)
    if start_ts >= end_ts:
        start_ts = end_ts - max(pad_before + pad_after, 24 * seconds)
    bars = research.fetch_coinbase_candles_range(
        gran,
        start_ts,
        end_ts,
        product_id=str(facts["product_id"]),
    )
    if not bars:
        raise RuntimeError(f"No candles for {facts['product_id']} {label}")
    return bars, label


def _tv_style():
    colors = mpf.make_marketcolors(
        up="#26a69a",
        down="#ef5350",
        edge="inherit",
        wick="inherit",
        volume={"up": "#26a69a", "down": "#ef5350"},
    )
    return mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=colors,
        facecolor=_FIG_BG,
        figcolor=_FIG_BG,
        edgecolor="#2a2e39",
        gridcolor=_GRID,
        gridstyle="-",
        y_on_right=True,
        rc={
            "axes.labelcolor": _TEXT,
            "axes.edgecolor": "#2a2e39",
            "xtick.color": _MUTED,
            "ytick.color": _MUTED,
            "text.color": _TEXT,
            "font.size": 9,
            "figure.facecolor": _FIG_BG,
            "savefig.facecolor": _FIG_BG,
        },
    )


def _to_mpf_df(bars: list[dict]):
    from charts import _to_mpf_df as convert

    return convert(bars)


def _bar_index(df, ts: str | None, price: float | None = None) -> int:
    from charts import _bar_index as nearest

    if ts:
        try:
            return nearest(df, ts)
        except Exception:
            pass
    if price is None:
        return max(0, len(df) - 1)
    # Fall back to the bar whose range contains the level, else closest close.
    target = float(price)
    for i, (_, row) in enumerate(df.iterrows()):
        lo = float(row["Low"])
        hi = float(row["High"])
        if lo <= target <= hi:
            return i
    closes = (df["Close"] - target).abs()
    return int(closes.values.argmin())


def _datetime_format(df) -> str:
    span = df.index[-1] - df.index[0]
    seconds = span.total_seconds() if hasattr(span, "total_seconds") else 0
    if seconds > 36 * 3600:
        return "%b %d"
    return "%H:%M"


def _place_xybox(slot: Slot, n_left: int, left_i: int) -> tuple[float, float]:
    """Axes-fraction anchors so every chart uses the same callout layout."""
    if slot.place == "tr":
        return 0.74, 0.82
    if slot.place == "tc":
        return 0.40, 0.90
    if slot.place == "left":
        # Stack TP boxes down the left margin.
        y = 0.62 - left_i * 0.16
        return 0.02, max(0.28, y)
    if slot.place == "bl":
        return 0.02, 0.12
    return 0.70, 0.10  # br / misc


def _offset_box(slot: Slot):
    title = TextArea(
        slot.title,
        textprops={
            "color": slot.color,
            "fontweight": "bold",
            "fontsize": 8.0,
            "fontfamily": "DejaVu Sans",
        },
    )
    body = TextArea(
        textwrap.fill(slot.body, width=_BODY_WRAP),
        textprops={
            "color": "white",
            "fontsize": 7.4,
            "fontfamily": "DejaVu Sans",
        },
    )
    children = [title, body]
    if slot.footer:
        children.append(
            TextArea(
                textwrap.fill(slot.footer, width=_BODY_WRAP),
                textprops={
                    "color": _FOOTER,
                    "fontsize": 7.4,
                    "fontweight": "bold",
                    "fontfamily": "DejaVu Sans",
                },
            )
        )
    return VPacker(children=children, align="left", pad=0, sep=3)


def render_case_study(
    facts: dict[str, Any],
    slots: list[Slot],
    bars: list[dict],
    *,
    tf_label: str,
    out_path: Path,
) -> Path:
    df = _to_mpf_df(bars)
    if df.empty:
        raise RuntimeError("empty OHLC for case study")

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=_tv_style(),
        volume=True,
        title="",
        ylabel="",
        ylabel_lower="",
        figsize=_FIGSIZE,
        datetime_format=_datetime_format(df),
        xrotation=0,
        returnfig=True,
        warn_too_much_data=2000,
        scale_padding={"left": 0.28, "right": 0.28, "top": 0.22, "bottom": 0.12},
    )
    ax = axes[0]
    lo = float(df["Low"].min())
    hi = float(df["High"].max())
    pad = (hi - lo) * 0.16 if hi > lo else abs(hi) * 0.01
    ax.set_ylim(lo - pad, hi + pad * 1.15)
    ax.set_facecolor(_FIG_BG)
    fig.patch.set_facecolor(_FIG_BG)

    product = facts["product_label"]
    market = facts["product_id"]
    side = facts["side"].upper()
    date = _fmt_date(facts.get("opened_at")) or _fmt_date(facts.get("closed_at"))
    fig.text(
        0.02,
        0.975,
        "Eva Trade — Case Study",
        color="white",
        fontsize=18,
        fontweight="bold",
        fontfamily="DejaVu Sans",
        va="top",
    )
    fig.text(
        0.02,
        0.935,
        f"{market}  ·  {side}  ·  {date}  ·  {tf_label}  ·  Coinbase",
        color=_MUTED,
        fontsize=10,
        fontfamily="DejaVu Sans",
        va="top",
    )
    pnl = float(facts.get("pnl_usd") or 0)
    pnl_color = "#3fb950" if pnl >= 0 else "#f85149"
    fig.text(
        0.98,
        0.975,
        f"{side}  {pnl:+,.0f} USD  ({float(facts.get('pnl_pct') or 0):+.1f}%)",
        color=pnl_color,
        fontsize=12,
        fontweight="bold",
        fontfamily="DejaVu Sans",
        va="top",
        ha="right",
    )

    left_i = 0
    for slot in slots:
        x = _bar_index(df, slot.ts, slot.price)
        y = float(slot.price)
        ax.scatter(
            [x],
            [y],
            s=36,
            c=slot.color,
            zorder=7,
            edgecolors=_FIG_BG,
            linewidths=0.8,
        )
        if slot.kind in {"tp", "exit", "stopped"}:
            ax.plot(
                [0, x],
                [y, y],
                color=slot.color,
                lw=0.9,
                alpha=0.75,
                zorder=5,
            )
        xybox = _place_xybox(slot, 0, left_i)
        if slot.place == "left":
            left_i += 1
        packed = _offset_box(slot)
        ab = AnnotationBbox(
            packed,
            xy=(x, y),
            xybox=xybox,
            xycoords="data",
            boxcoords="axes fraction",
            box_alignment=(0.0, 0.5) if slot.place in {"left", "bl", "tr", "br"} else (0.5, 0.0),
            bboxprops={
                "boxstyle": "round,pad=0.55",
                "fc": _BOX_FACE,
                "ec": slot.color,
                "lw": 1.35,
                "alpha": 0.96,
            },
            arrowprops={
                "arrowstyle": "-|>",
                "color": slot.color,
                "lw": 1.15,
                "shrinkA": 4,
                "shrinkB": 2,
            },
            frameon=True,
            pad=0.25,
            zorder=8,
        )
        ax.add_artist(ab)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        out_path,
        dpi=_DPI,
        facecolor=_FIG_BG,
        edgecolor=_FIG_BG,
        bbox_inches="tight",
        pad_inches=0.18,
    )
    plt.close(fig)
    return out_path


def generate_for_trade(
    trade_id: int, *, rationale: str | None = None
) -> Path | None:
    """Build facts → copy → PNG and persist the path. Returns None on skip/fail.

    ``rationale`` is the original entry thesis. When omitted it is loaded from
    the originating cycle. Pass it in when the caller already has it so copy
    generation does not depend on a second ledger lookup.
    """
    if not bot_config.CASE_STUDY_ENABLED:
        return None
    row = live_ledger.get_trade(trade_id)
    if not row:
        return None
    if str(row.get("source") or "") != "hq":
        return None
    if str(row.get("status") or "") != "closed":
        return None

    existing = row.get("case_study_path")
    if existing:
        from dashboard.charts import resolve_chart_path

        resolved = resolve_chart_path(str(existing))
        if resolved is not None:
            return resolved

    try:
        facts = build_facts(row, rationale=rationale)
        if not facts.get("rationale"):
            logger.info(
                "Case study #%s has no stored entry rationale; copy will be generic",
                trade_id,
            )
        bars, tf_label = fetch_bars(facts)
        facts["stop_touched"] = _stop_touched(facts, bars)
        facts["post_exit"] = _post_exit_stats(facts, bars)
        facts["timeframe"] = tf_label
        copy = llm_copy(facts) or {}
        fallback = fallback_copy(facts)
        merged = {
            "bodies": {**fallback.get("bodies", {}), **(copy.get("bodies") or {})},
            "misc_title": copy.get("misc_title") or fallback["misc_title"],
            "misc_body": copy.get("misc_body") or fallback["misc_body"],
        }
        slots = build_slots(facts, copy=merged)
        filename = f"case_study_hq_{trade_id}.png"
        out_path = config.CHARTS_DIR / filename
        render_case_study(facts, slots, bars, tf_label=tf_label, out_path=out_path)
        rel = f"charts/{filename}"
        live_ledger.set_case_study_path(trade_id, rel)
        logger.info("Case study written for HQ trade #%s → %s", trade_id, rel)
        return out_path
    except Exception:
        logger.exception("Case study generation failed for HQ trade #%s", trade_id)
        return None


def queue_generate(trade_id: int, *, rationale: str | None = None) -> None:
    """Fire-and-forget so `_close_out` never waits on candles or Claude."""
    if not bot_config.CASE_STUDY_ENABLED:
        return
    with _INFLIGHT_LOCK:
        if trade_id in _INFLIGHT:
            return
        _INFLIGHT.add(trade_id)

    def _run() -> None:
        try:
            generate_for_trade(trade_id, rationale=rationale)
        except Exception:
            logger.exception("Case study thread failed for trade #%s", trade_id)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(trade_id)

    threading.Thread(
        target=_run, daemon=True, name=f"eva-case-study-{trade_id}"
    ).start()


def backfill_missing(limit: int = 1) -> None:
    """Generate charts for already-closed HQ trades that do not have one yet."""
    if not bot_config.CASE_STUDY_ENABLED:
        return
    from dashboard.charts import resolve_chart_path

    queued = 0
    for row in live_ledger.get_closed_trades(limit=25, source="hq"):
        if queued >= limit:
            return
        path = row.get("case_study_path")
        if path and resolve_chart_path(str(path)) is not None:
            continue
        queue_generate(int(row["id"]))
        queued += 1
