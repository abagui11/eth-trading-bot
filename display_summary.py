"""Post-audit Telegram display summaries — never mutate Suggestion.rationale."""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

import anthropic

import bot_config
import config
from analyze import log_anthropic_usage
from critic import split_rationale
from models import Suggestion

logger = logging.getLogger(__name__)

LONG_ACTIONS = {"spot_buy", "deriv_buy"}
SHORT_ACTIONS = {"spot_sell", "deriv_sell"}

SUMMARY_SYSTEM = """You write short, plain-language trade-card blurbs for retail users.
You receive an audited trading thesis. Rewrite it as 1-2 sentences.

Rules:
- Explain the setup in everyday ICT/swing language (structure, order block, fib, liquidity).
- Do NOT invent prices, percentages, R/R, dates, or levels.
- Do NOT invent liquidity sweeps, SFPs, or HTF bias unless clearly present in the thesis.
- Do NOT say "guaranteed", "will", or give financial advice.
- Prefer "suggests", "lines up with", "points to", "may".
- No bullet lists. No markdown. No numbers at all.
- Return plain text only (no JSON, no quotes wrapping the whole reply)."""

_NUM_RE = re.compile(r"\d")
_MAX_SUMMARY_CHARS = 320

# What a subscriber would call the order that actually goes on the book. Spot
# and futures share the limit mechanics, so the part worth spelling out is the
# exposure each one opens — "sell" on a contract you do not hold is the bit
# people read as closing something.
RESTING_ORDER_TERMS = {
    "spot_buy": "spot limit buy",
    "spot_sell": "spot limit sell",
    "deriv_buy": "futures limit buy that opens a long",
    "deriv_sell": "futures limit sell that opens a short",
}

MARKET_ORDER_TERMS = {
    "spot_buy": "spot market buy",
    "spot_sell": "spot market sell",
    "deriv_buy": "futures market buy that opens a long",
    "deriv_sell": "futures market sell that opens a short",
}


def side_label(action: str) -> str:
    if action in LONG_ACTIONS:
        return "long"
    if action in SHORT_ACTIONS:
        return "short"
    return "flat"


def friendly_title(suggestion: Suggestion) -> str:
    product = bot_config.product_label(suggestion.product_id)
    action = suggestion.action.lower()
    if action in ("spot_buy", "deriv_buy"):
        kind = "Spot Buy" if action.startswith("spot") else "Deriv Buy"
    elif action in ("spot_sell", "deriv_sell"):
        kind = "Spot Sell" if action.startswith("spot") else "Deriv Sell"
    else:
        kind = "No Trade"
    return f"{product} {kind}"


def is_watchdog_suggestion(suggestion: Suggestion) -> bool:
    return "[Watchdog" in (suggestion.rationale or "")


def is_scale_in(suggestion: Suggestion) -> bool:
    tranche = (suggestion.entry_tranche or "").strip()
    if tranche == "0.718":
        return True
    rationale = suggestion.rationale or ""
    return "Scale-in" in rationale or "adds 25% notional" in rationale.lower()


def price_move_pcts(suggestion: Suggestion) -> dict[str, float] | None:
    """Return the % gain at TP1 and the % loss at SL, both from entry.

    These are position returns, not price moves: on a short, TP1 sits *below*
    entry and still returns `+tp_pct`. The card has to name the direction price
    travels separately — see ``price_direction`` — or the sign reads as a
    claim that price rises into a short's target.
    """
    if suggestion.entry is None or suggestion.stop_loss is None:
        return None
    if not suggestion.take_profits:
        return None
    entry = float(suggestion.entry)
    stop = float(suggestion.stop_loss)
    tp1 = float(suggestion.take_profits[0])
    if entry <= 0:
        return None
    side = side_label(suggestion.action)
    if side == "long":
        tp_pct = (tp1 - entry) / entry * 100.0
        sl_pct = (entry - stop) / entry * 100.0
    elif side == "short":
        tp_pct = (entry - tp1) / entry * 100.0
        sl_pct = (stop - entry) / entry * 100.0
    else:
        return None
    return {"tp_pct": tp_pct, "sl_pct": sl_pct}


def format_pct(value: float) -> str:
    return f"{value:+.2f}%"


def price_direction(action: str, *, toward_target: bool) -> str:
    """Which way price has to travel to reach a target or a stop.

    A short's target is a fall and its stop is a rise, which is the opposite of
    what its `+`/`-` return sign looks like at a glance.
    """
    rising = (side_label(action) == "long") == toward_target
    return "rises" if rising else "falls"


def source_timestamp(suggestion: Suggestion) -> str | None:
    """Best-effort setup timestamp for decision-chart windowing."""
    ob = suggestion.order_block or {}
    for key in ("start_ts", "displacement_ts", "end_ts"):
        raw = ob.get(key)
        if raw:
            return str(raw)
    return None


def deterministic_setup_blurb(suggestion: Suggestion) -> str:
    """Fallback setup sentence when LLM summary is unavailable."""
    side = side_label(suggestion.action)
    if side == "flat":
        return "No actionable setup this cycle."

    bias = "bullish" if side == "long" else "bearish"
    if is_scale_in(suggestion):
        return (
            f"Adding to the existing {bias} position where the M5 fib add "
            f"level lines up with the open idea."
        )
    if is_watchdog_suggestion(suggestion):
        return (
            f"We see {bias} structure on the higher timeframe that lines up "
            f"with a fib retracement on M5."
        )
    return (
        f"Higher-timeframe {bias} structure aligns with an M5 order-block "
        f"fib entry for this idea."
    )


def _thesis_for_prompt(suggestion: Suggestion) -> str:
    body, _ = split_rationale(suggestion.rationale or "")
    text = (body or suggestion.rationale or "").strip()
    return text[:2500]


def _validate_llm_summary(text: str) -> str | None:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return None
    if len(cleaned) > _MAX_SUMMARY_CHARS:
        cleaned = cleaned[:_MAX_SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(".,;:") + "."
    if _NUM_RE.search(cleaned):
        return None
    # Reject advice / certainty tells that often invent outcomes.
    lowered = cleaned.lower()
    banned = ("guaranteed", "will hit", "financial advice", "buy now", "sell now")
    if any(token in lowered for token in banned):
        return None
    return cleaned


def generate_llm_setup_blurb(suggestion: Suggestion) -> str | None:
    """Ask the model for a number-free setup sentence. Returns None on failure."""
    if suggestion.action == "no_trade":
        return None
    thesis = _thesis_for_prompt(suggestion)
    if not thesis:
        return None

    product = bot_config.product_label(suggestion.product_id)
    side = side_label(suggestion.action)
    user_prompt = (
        f"Product: {product}\n"
        f"Side: {side}\n"
        f"Watchdog: {'yes' if is_watchdog_suggestion(suggestion) else 'no'}\n"
        f"Scale-in: {'yes' if is_scale_in(suggestion) else 'no'}\n\n"
        f"Audited thesis:\n{thesis}"
    )

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL_FAST,
            max_tokens=220,
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        log_anthropic_usage(response, "display_summary")
    except Exception:
        logger.exception("Display summary LLM call failed")
        return None

    raw = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            raw += block.text
    return _validate_llm_summary(raw)


def generate_display_summary(suggestion: Suggestion) -> str:
    """Return friendly setup prose; never mutates suggestion.rationale."""
    if bot_config.USE_LLM_DISPLAY_SUMMARY:
        llm = generate_llm_setup_blurb(suggestion)
        if llm:
            return llm
    return deterministic_setup_blurb(suggestion)


class ExecutionNote(NamedTuple):
    """The card's opening line, split so only the verdict is emphasised."""

    headline: str
    detail: str

    @property
    def text(self) -> str:
        return f"{self.headline} {self.detail}".strip()


def execution_banner(
    suggestion: Suggestion,
    *,
    spot: float | None = None,
    resting: bool | None = None,
) -> ExecutionNote | None:
    """Say whether this card is an order still waiting or an entry going on now.

    Eva's entry is a pullback into an M5 block, so the market is usually not
    there yet and most HQ cards are orders that may never fill. "Potential
    entry near $X" does not carry that — it reads like a position someone
    already holds, which is the one thing about these broadcasts that misleads.

    ``resting`` is the same verdict ``agent`` routed the trade on, so a card can
    never claim a fill the executor did not attempt. ``None`` means the caller
    does not know which path was taken, and the card stays quiet rather than
    guessing.
    """
    if resting is None or suggestion.action == "no_trade" or suggestion.entry is None:
        return None

    product = bot_config.product_label(suggestion.product_id)
    entry = float(suggestion.entry)
    mark = float(spot) if spot and float(spot) > 0 else None
    add = "Adding to the open position. " if is_scale_in(suggestion) else ""

    if not resting:
        term = MARKET_ORDER_TERMS.get(suggestion.action, "market order")
        at = f" near ${mark:,.2f}" if mark else ""
        return ExecutionNote(
            headline=f"{add}Going on now at market — this is not a resting order.",
            detail=(
                f"{product} has already reached the entry, so it fills "
                f"immediately{at} as a {term}."
            ),
        )

    term = RESTING_ORDER_TERMS.get(suggestion.action, "limit order")
    moves = "falls" if side_label(suggestion.action) == "long" else "rises"
    gap = ""
    if mark:
        gap = f" from ${mark:,.2f} ({abs(entry - mark) / mark * 100.0:.2f}% away)"
    return ExecutionNote(
        headline=f"{add}Not filled yet — a potential entry that may never fill.",
        detail=(
            f"It executes only if {product} {moves} to ${entry:,.2f}{gap}, and "
            f"rests as a {term} until then."
        ),
    )


def build_card_body(
    suggestion: Suggestion,
    *,
    display_summary: str | None = None,
    telegram_id: int | None = None,
    offer_id: str | None = None,
    resting: bool | None = None,
    spot: float | None = None,
) -> str:
    """Concise photo caption / card text (Telegram caption limit 1024)."""
    if suggestion.action == "no_trade":
        return "NO TRADE — tap See more for the full rationale."

    if suggestion.entry is None or suggestion.stop_loss is None:
        return friendly_title(suggestion)

    pcts = price_move_pcts(suggestion)
    summary = (display_summary or "").strip() or deterministic_setup_blurb(suggestion)

    entry = float(suggestion.entry)
    stop = float(suggestion.stop_loss)
    tp1 = float(suggestion.take_profits[0]) if suggestion.take_profits else None

    # HQ hourly (abstention-first ICT) cards carry the High Quality label;
    # watchdog programmatic fires do not.
    title = friendly_title(suggestion)
    if not is_watchdog_suggestion(suggestion):
        title = f"High Quality · {title}"

    # The banner already names the entry and says what happens to it, so the
    # old lead would only repeat the price under a vaguer verb.
    note = execution_banner(suggestion, spot=spot, resting=resting)
    if note is not None:
        # A paragraph, not a one-liner, so it needs air before the levels.
        lines = [title, "", note.text, ""]
    elif is_scale_in(suggestion):
        lines = [title, "", f"Adding near ${entry:,.2f}."]
    else:
        lines = [title, "", f"Potential entry near ${entry:,.2f}."]
    if pcts and tp1 is not None:
        # The percentages are position returns, so on a short the target is a
        # "+" that price has to fall into. Naming the direction is the whole
        # point of these two clauses — "+2.07% price move" read as a rise.
        lines.append(
            f"Target 1 is ${tp1:,.2f} "
            f"({format_pct(pcts['tp_pct'])} if price "
            f"{price_direction(suggestion.action, toward_target=True)} to it), "
            f"with a stop at ${stop:,.2f} "
            f"({format_pct(-abs(pcts['sl_pct']))} if price "
            f"{price_direction(suggestion.action, toward_target=False)} to it)."
        )
    else:
        tps = ", ".join(f"${tp:,.2f}" for tp in suggestion.take_profits[:3]) or "n/a"
        lines.append(f"SL: ${stop:,.2f} · TP: {tps}")

    lines.extend(["", summary])

    # Account-aware sizing: live pool risk when the tester pool is on,
    # otherwise the personal demo book.
    import user_books  # local import avoids circular import at module load

    if telegram_id is not None and tp1 is not None:
        if bot_config.POOL_ENABLED:
            import pool

            if pool.is_approved(int(telegram_id)):
                prosp = pool.prospective_accept(
                    int(telegram_id), entry=entry, stop_loss=stop
                )
                lines.append("")
                if prosp.get("ok"):
                    risk_pct = float(prosp["risk_pct"]) * 100
                    lines.append(
                        f"Your Accept ≈ ${float(prosp['risk_usd']):,.2f} at risk "
                        f"({risk_pct:.1f}% of ${float(prosp['available_usd']):,.0f} available)"
                    )
                    lines.append(
                        f"≈ ${float(prosp['notional_usd']):,.0f} position size at this stop"
                    )
                elif prosp.get("reason") == "below_min_equity":
                    lines.append(
                        f"Fund at least ${float(prosp.get('minimum_usd') or 0):,.0f} "
                        "(/deposit) to join live Accepts."
                    )
                elif prosp.get("reason") == "no_available_cash":
                    lines.append(
                        "No available cash right now — other Accepts may be reserved. "
                        "/portfolio for the breakdown."
                    )
                else:
                    lines.append(
                        "Fund with /deposit to join this trade with live size "
                        f"(about {bot_config.POOL_RISK_PCT * 100:.1f}% of available "
                        "cash at risk per Accept)."
                    )
            else:
                lines.append("")
                lines.append("Access required to Accept with live size.")
        else:
            sizing = user_books.compute_user_notional(
                telegram_id,
                entry,
                deploy_pct=suggestion.deploy_pct,
            )
            if sizing.get("ok"):
                rr_usd = user_books.prospective_risk_reward_usd(
                    entry=entry,
                    stop_loss=stop,
                    take_profit=tp1,
                    side=side_label(suggestion.action),
                    notional_usd=float(sizing["notional_usd"]),
                )
                lines.append("")
                lines.append(f"Your demo size ≈ ${float(sizing['notional_usd']):,.0f}")
                lines.append(
                    f"Est. TP1 ≈ ${rr_usd['reward_usd']:,.0f} · "
                    f"Est. SL ≈ ${rr_usd['risk_usd']:,.0f}"
                )
            else:
                lines.append("")
                lines.append("Open a demo account to Accept with your demo cash.")
    elif suggestion.size:
        lines.append("")
        lines.append(f"Agent size: ${suggestion.size:,.2f}")

    window = int(bot_config.APPROVAL_WINDOW_MIN)
    if offer_id:
        lines.append("")
        lines.append(f"Accept within {window} min · offer {offer_id[-8:]}")

    return "\n".join(lines)[:1024]


def build_detail_levels_block(suggestion: Suggestion) -> str:
    """Exact levels block for the See more follow-up."""
    tps = ", ".join(f"{tp:,.2f}" for tp in suggestion.take_profits[:3]) or "n/a"
    rr = f"{suggestion.risk_reward:.2f}" if suggestion.risk_reward is not None else "n/a"
    prefix = "WATCHDOG — " if is_watchdog_suggestion(suggestion) else ""
    return "\n".join(
        [
            f"{prefix}{suggestion.action.upper()} · "
            f"{bot_config.product_label(suggestion.product_id)}",
            f"Entry: {suggestion.entry:,.2f}" if suggestion.entry is not None else "Entry: n/a",
            f"SL: {suggestion.stop_loss:,.2f}"
            if suggestion.stop_loss is not None
            else "SL: n/a",
            f"TP: {tps}",
            f"R/R: {rr}",
        ]
    )
