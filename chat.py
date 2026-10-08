"""Claude Q&A grounded on the user's pool account and latest suggestions."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import anthropic

import analyze
import audit
import bot_config
import config
import ledger
import research
import telegram_text

logger = logging.getLogger(__name__)

_HISTORY_CYCLES = 36

_SYSTEM_SUFFIX = f"""
You are Eva, the Telegram trading assistant for a deploy-into-strategies product.

Product model (ground truth — do not contradict):
- Users get admitted, Fund USDC, optionally register a payout wallet via /wallet,
  then deploy into strategies. Funded USDC stays undeployed until they allocate
  to a strategy. Accept/Reject trade cards; Kalshi Accepts spend at most
  {bot_config.POOL_KALSHI_RISK_PCT * 100:.0f}% of that strategy's deployment
  (contract cost = entire risk); ICT/Mill Accepts risk about
  {bot_config.POOL_RISK_PCT * 100:.1f}% at the stop — never the full wallet.
  Autopilot auto-Accepts every trade on that lane.
- Strategies: ICT Trades (ict), Trade Mill (mill), Kalshi 15m Reversal
  (kalshi_reversal), Kalshi 15m Wick (kalshi_wick). Phase 1 pickers show
  Kalshi 15m Wick only.
- Fund / Wallet / Strategies / Portfolio / Brain are the main surfaces.
- There is no shared simulated paper portfolio for subscribers. Do not invent a
  $5,000 paper book, claim this bot "only tracks paper trades", or say there is
  no wallet registration feature.

Answer about:
- Funding, wallet registration/verification, allocations, Accept risk, and the
  user's portfolio — using the "Your account" block when present as ground truth
- Strategies and how deploy / Accept works
- The current or latest trade suggestion and ledger history (cycle IDs, rationales)
- ICT / Trading Guide concepts for market questions

If a "Your account" block is present, treat it as fact for that user (access,
wallet registered?, balances, subscriptions, open stakes). If they ask whether
their wallet is registered, answer from that block.

If access is pending / denied / no account, explain Admit / Fund — never fall
back to a house paper portfolio.

Trading Guide language about paper equity is house control sizing for idea
construction only — not this user's capital. User balances come only from
"Your account".

If the context includes trade update history or search matches, use those for
questions like "which update said X" — cite the cycle_id and timestamp.

"What did my past trades look like" / "how have I done": answer from the
"Your settled Kalshi trades" list and lane totals in "Your account" (dates,
sides, entries, per-trade P&L). Report winners and losers alike. The list
shows the most recent trades; the lane totals are lifetime — use the totals
for any overall figure. Recent ICT/Mill closes appear under "Recent closed".

Hypothetical performance questions ("how much would I have made on autopilot
since my funds arrived", "what did I miss?"): when the "Autopilot what-if"
block is present in "Your account", answer from it — quote its actual and
estimated figures exactly, and always carry its caveat (an estimate that
assumes house-price fills; real autopilot skips slipped windows, so the live
figure runs lower). Never do your own what-if arithmetic beyond that block —
the full per-user fill series is not in your context, and invented figures
will be stripped by the audit. If the block is absent, say you can only
report what is recorded, quote realized/unrealized PnL and recent closed
trades from "Your account", and point them to Portfolio.

Be concise and practical. For market context digests and historical pattern
research, direct users to /research (topic catalog). Examples:
- /research digest — full market snapshot
- /research macro, funding, volume, dominance, miner — individual topics
- /research asian_session — Asian session (9pm–4am ET) net change 2w/4w/2m
- /research h12_sfp, weekly_sfp, d1_sfps — SFP studies with charts
- Add ETH or BTC (default ETH); e.g. /research d1_sfps 5 BTC

When an authoritative cycle snapshot is provided, spot, zones, SFPs, and key
levels in your answer MUST match that snapshot. Do not invent prices or zones
that contradict it.

Replies are delivered as plain Telegram text, so markdown is not rendered.
Write prose and short "- " bullet lists only: no #/## headings, no **bold** or
*italic*, no `code`, no tables, no --- rules. Do not end the reply with a PnL
or portfolio dump — point users to Fund, Wallet, Strategies, or Portfolio.

This is not financial advice.
"""


def _format_suggestion_context(row: dict) -> str:
    tps = ", ".join(f"{tp:,.2f}" for tp in row.get("take_profits", [])) or "n/a"
    return (
        f"Latest suggestion (cycle {row['cycle_id']}, {row['ts']}):\n"
        f"  action: {row['action']}\n"
        f"  entry: {row.get('entry')}\n"
        f"  stop_loss: {row.get('stop_loss')}\n"
        f"  take_profits: {tps}\n"
        f"  risk_reward: {row.get('risk_reward')}\n"
        f"  price_at_suggestion: {row.get('price_at_suggestion')}\n"
        f"  rationale: {row.get('rationale', '')}\n"
        f"  setup_tags: {row.get('setup_tags') or 'n/a'}\n"
        f"  chart_path: {row.get('chart_path')}"
    )


def _pick_chart_path(*candidates: str | None) -> str | None:
    for path in candidates:
        if path and Path(path).exists():
            return path
        if path and "," in path:
            for part in path.split(","):
                part = part.strip()
                if part and Path(part).exists():
                    return part
    return None


def _search_terms_from_message(message: str) -> list[str]:
    """Extract meaningful phrases for ledger rationale search."""
    cleaned = re.sub(r"[^\w\s$.,%-]", " ", message)
    words = [w for w in cleaned.split() if len(w) >= 3]
    if not words:
        return []
    terms: list[str] = []
    if len(words) >= 3:
        terms.append(" ".join(words[:6]))
    for w in words:
        if w.lower() not in {
            "what", "when", "which", "where", "that", "this", "said", "trade",
            "update", "about", "from", "have", "were", "was", "the", "and",
        }:
            terms.append(w)
    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique[:4]


def _strategy_label(key: str) -> str:
    try:
        import strategy_catalog
        strat = strategy_catalog.STRATEGIES.get(key)
        return strat.label if strat else key
    except Exception:
        return key


def _format_user_account_context(telegram_id: int) -> str:
    """Compact plain-text snapshot of this user's pool account for the LLM."""
    lines = ["=== Your account ==="]

    if not bot_config.POOL_ENABLED:
        lines.append(
            "Pool product is off in this deployment. Personal demo accounts "
            "may still apply; do not invent a shared house paper portfolio."
        )
        return "\n".join(lines)

    import pool

    status = pool.access_status(telegram_id)
    if status is None:
        lines.append("Access: not requested yet (send any message or /start to request Admit).")
        return "\n".join(lines)
    if status == "pending":
        lines.append("Access: pending admin Admit.")
        return "\n".join(lines)
    if status == "denied":
        lines.append("Access: denied.")
        return "\n".join(lines)
    if status != "approved":
        lines.append(f"Access: {status}")
        return "\n".join(lines)

    lines.append("Access: approved")

    wallet = pool.get_wallet(telegram_id)
    if wallet is None:
        lines.append(
            "Payout wallet: not registered. "
            "Register with /wallet 0x… (needed for withdrawals; funding via Fund does not require it)."
        )
    else:
        wstatus = str(wallet.get("status") or "pending")
        addr = str(wallet.get("address") or "")
        lines.append(f"Payout wallet: {addr} ({wstatus})")
        change = pool.get_wallet_change_request(telegram_id)
        if change:
            lines.append(f"Wallet change pending review: {change.get('address')}")

    try:
        p = pool.portfolio(telegram_id)
    except Exception:
        logger.exception("chat: portfolio failed for %s", telegram_id)
        p = {"ok": False}

    if not p.get("ok"):
        lines.append("Pool account: none yet — Fund after Admit to open one.")
        return "\n".join(lines)

    wallet_usd = float(
        p["wallet_usd"] if p.get("wallet_usd") is not None
        else max(0.0, float(p.get("cash_usd") or 0) - float(p.get("deployed_usd") or 0))
    )
    deployed = float(p.get("deployed_usd") or 0)
    cash = float(p.get("cash_usd") or 0)
    total = float(p.get("total_usd") or (wallet_usd + deployed))
    reserved = float(p.get("reserved_usd") or 0)
    realized = float(p.get("realized_pnl_usd") or 0)
    unrealized = float(p.get("unrealized_pnl_usd") or 0)

    lines.append(f"Total: ${total:,.2f}")
    lines.append(f"  Undeployed wallet: ${wallet_usd:,.2f}")
    lines.append(f"  Deployed: ${deployed:,.2f}")
    lines.append(f"  Cash claim: ${cash:,.2f}")
    if reserved > 0:
        lines.append(f"  Reserved in open trades: ${reserved:,.2f}")
    lines.append(f"PnL: realized ${realized:+,.2f} · unrealized ${unrealized:+,.2f}")

    subs = pool.strategy_subscriptions(telegram_id)
    if subs:
        lines.append("Subscriptions: " + ", ".join(_strategy_label(s) for s in subs))
    else:
        lines.append("Subscriptions: none — use Strategies or /subscribe")

    allocs = pool.allocations(telegram_id)
    active = {k: v for k, v in allocs.items() if float(v) > 0}
    if active:
        lines.append("Allocations:")
        for key, amt in active.items():
            lines.append(f"  - {_strategy_label(key)} ({key}): ${float(amt):,.2f}")
    else:
        lines.append("Allocations: none — deploy capital via Strategies before Accept sizing applies")

    opens = p.get("open_stakes") or []
    if opens:
        lines.append(f"Open stakes ({len(opens)}):")
        for s in opens[:10]:
            product = bot_config.product_label(str(s.get("product_id") or ""))
            unreal = s.get("unrealized_usd")
            unreal_bit = f" · unrealized ${float(unreal):+,.2f}" if unreal is not None else ""
            lines.append(
                f"  - {product} {s.get('side')} cost ${float(s.get('cost_usd') or 0):,.2f} "
                f"risk ${float(s.get('risk_usd') or 0):,.2f}{unreal_bit}"
            )

    kalshi_open = p.get("kalshi_open") or []
    if kalshi_open:
        lines.append(f"Kalshi open ({len(kalshi_open)}):")
        for k in kalshi_open[:10]:
            lines.append(
                f"  - {k.get('market_ticker')} {str(k.get('side', '')).upper()} "
                f"x{int(k.get('contracts') or 0)} @ {float(k.get('entry_cents') or 0):.0f}c "
                f"${float(k.get('cost_usd') or 0):,.2f}"
            )

    closed = (p.get("closed_stakes") or [])[:5]
    if closed:
        lines.append("Recent closed (ICT/Mill):")
        for s in closed:
            product = bot_config.product_label(str(s.get("product_id") or ""))
            lines.append(
                f"  - {product} {s.get('side')} ${float(s.get('realized_pnl_usd') or 0):+,.2f}"
            )

    # Full-enough settled Kalshi history for "what did my past trades look
    # like" — dates, entries, and P&L, plus lifetime per-lane totals so the
    # list being capped never makes the quoted total wrong.
    try:
        settled = [
            r for r in pool.kalshi_positions_for(telegram_id, limit=40)
            if str(r.get("status")) == "settled"
        ]
    except Exception:
        logger.exception("chat: kalshi history failed for %s", telegram_id)
        settled = []
    if settled:
        lanes = sorted({str(r.get("strategy")) for r in settled})
        totals = []
        for lane in lanes:
            t = pool.kalshi_lane_realized(telegram_id, lane)
            totals.append(
                f"{_strategy_label(lane)}: {t['settled']} settled, "
                f"${t['pnl_usd']:+,.2f} lifetime"
            )
        lines.append("Your Kalshi lane totals: " + " · ".join(totals))
        lines.append(f"Your settled Kalshi trades (most recent {min(len(settled), 15)}):")
        for r in settled[:15]:
            when = str(r.get("settled_at") or r.get("created_at") or "")[:16]
            lines.append(
                f"  - {when} {r.get('market_ticker')} "
                f"{str(r.get('side', '')).upper()} x{int(r.get('contracts') or 0)} "
                f"@ {float(r.get('entry_cents') or 0):.0f}c "
                f"→ ${float(r.get('pnl_usd') or 0):+,.2f}"
            )

    if p.get("frozen"):
        lines.append("Note: new joins are paused while books are verified; exits still book.")

    # Recorded autopilot counterfactual ("what did I miss?"): actual settled
    # lane PnL vs a replay of every house trade sized like this user's
    # deployment. Estimate by construction — the lines name the assumptions,
    # and the system prompt requires quoting them with the figures.
    try:
        import counterfactual
        import telegram_ui
        for strategy in allocs:
            if not str(strategy).startswith("kalshi"):
                continue
            what_if = counterfactual.autopilot_what_if(telegram_id, strategy)
            what_if_lines = telegram_ui.format_autopilot_what_if(what_if)
            if what_if_lines:
                lines.append("")
                lines.append(
                    "Autopilot what-if (recorded counterfactual — quote these "
                    "figures only, with the estimate caveat):"
                )
                lines.extend(what_if_lines)
    except Exception:
        logger.exception("chat: autopilot what-if failed for %s", telegram_id)

    return "\n".join(lines)


def account_facts(telegram_id: int) -> str:
    """Public alias used by the chat audit: the same account block the model
    answered from, handed to the critic as ground truth so true claims about
    this user's trades and balances are never flagged against the market
    snapshot."""
    return _format_user_account_context(telegram_id)


def _build_context(
    spot: float,
    user_message: str,
    telegram_id: int | None = None,
) -> tuple[str, str | None, dict[str, str]]:
    """Return (text context, optional ledger chart path, snapshot marked chart paths)."""
    parts: list[str] = [f"Current ETH spot: ${spot:,.2f}"]
    snapshot_charts: dict[str, str] = {}

    if telegram_id is not None:
        try:
            parts.append("")
            parts.append(_format_user_account_context(telegram_id))
        except Exception:
            logger.exception("chat: user account context failed for %s", telegram_id)
            parts.append("")
            parts.append("=== Your account ===\n(unavailable right now)")

    snapshot_row = audit.get_latest_snapshot()
    if snapshot_row:
        cycle_id = snapshot_row.get("cycle_id", "unknown")
        parts.append("")
        parts.append(f"=== Authoritative cycle snapshot ({cycle_id}) ===")
        ctx = audit.market_context_from_dict(snapshot_row["snapshot"])
        parts.append(ctx.summary_text)
        snapshot_charts = snapshot_row.get("marked_chart_paths") or {}

    try:
        from macro.context import build_macro_block

        macro_block = build_macro_block()
        if macro_block:
            parts.append("")
            parts.append(macro_block)
    except Exception:
        pass

    chart_path: str | None = None
    latest = ledger.get_latest_suggestion()
    if latest:
        parts.append("")
        parts.append(_format_suggestion_context(latest))
        chart_path = _pick_chart_path(latest.get("chart_path"))
    else:
        trade = ledger.get_latest_trade_suggestion()
        if trade:
            parts.append("")
            parts.append(_format_suggestion_context(trade))
            chart_path = _pick_chart_path(trade.get("chart_path"))

    history = ledger.get_latest(_HISTORY_CYCLES)
    if history:
        parts.append("")
        parts.append("=== Trade update history (ledger) ===")
        parts.append(ledger.format_history_summary(history))

    search_hits: list[dict] = []
    for term in _search_terms_from_message(user_message):
        search_hits.extend(ledger.search_rationale(term, limit=3))
    if search_hits:
        seen_ids: set[int] = set()
        deduped: list[dict] = []
        for row in search_hits:
            rid = int(row["id"])
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            deduped.append(row)
        if deduped:
            parts.append("")
            parts.append("=== Ledger search matches (for user question) ===")
            parts.append(ledger.format_history_summary(deduped[:8], max_rationale_chars=400))

    return "\n".join(parts), chart_path, snapshot_charts


def answer(user_message: str, telegram_id: int | None = None) -> str:
    """Return Claude's reply as plain Telegram text, grounded on this user when known."""
    guide = analyze.load_trading_guide()
    spot = research.get_spot_price()

    if ledger.get_latest_suggestion() is None and ledger.get_latest_trade_suggestion() is None:
        # Still answer account/wallet questions when we have a user id.
        if telegram_id is None:
            return (
                "No trade suggestions yet. The agent runs every hour — check back after the first cycle."
            )

    text_context, chart_path, snapshot_charts = _build_context(
        spot, user_message, telegram_id=telegram_id,
    )
    text_context = f"{text_context}\n\nUser question: {user_message}"

    live_chart_paths: dict[str, str] = {}
    for tf in ("H4", "M5"):
        path = snapshot_charts.get(tf)
        if path and Path(path).exists():
            live_chart_paths[tf] = path

    vision_blocks = analyze.build_vision_content(
        chart_paths=live_chart_paths or None,
        annotated_h1_path=(
            chart_path
            if not live_chart_paths and chart_path and Path(chart_path).exists()
            else None
        ),
        include_live_charts=bool(live_chart_paths),
    )

    user_content: list[dict] = [{"type": "text", "text": text_context}]
    user_content.extend(vision_blocks)

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=analyze._cached_system_blocks(guide, extra_suffix=_SYSTEM_SUFFIX),
            messages=[{"role": "user", "content": user_content}],
        )
        analyze.log_anthropic_usage(response, "chat")
    except Exception as exc:
        logger.exception("Chat Claude API call failed")
        return f"Sorry, I could not reach the analysis service right now. ({exc})"

    reply = ""
    for block in response.content:
        if block.type == "text":
            reply += block.text

    reply = telegram_text.to_plain_text(reply)
    return reply[:3500] if reply else "I don't have an answer for that right now."
