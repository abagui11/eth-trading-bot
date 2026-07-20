"""Claude Q&A about the latest trade suggestion."""

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
import paper
import research

logger = logging.getLogger(__name__)

_HISTORY_CYCLES = 36

_SYSTEM_SUFFIX = f"""
You are the ETH trading agent assistant. Answer only about:
- The agent's ICT swing strategy and Trading Guide
- Open paper positions (up to {bot_config.MAX_OPEN_TRADES} at once), including SL, TP, entry, size, unrealized P&L, exit plan
- Closed paper trades and realized P&L from the context provided
- The current or latest hourly trade suggestion
- Hourly trade update history in the ledger (timestamps, cycle IDs, rationales, chart paths)
- Paper portfolio performance shown in the PnL line

When positions are open, always reference their stop loss, take profit levels, and exit plan
from the open positions block — do not say SL/TP are missing if they appear there.

If the context includes trade update history or search matches, use those to answer questions
like "which update said X" or "what did we say about Y" — cite the cycle_id and timestamp.

If the context includes closed paper trades, use those for questions about past trades,
realized P&L, or closed positions (e.g. deriv_sell). Do not say history is unavailable
if closed trades appear in the context.

If the context includes a "Latest hourly cycle" section that differs from open positions,
explain both: what is live in paper vs what the most recent hourly analysis recommended.

Be concise and practical. For market context digests and historical pattern research,
direct users to /research (topic catalog). Examples:
- /research digest — full market snapshot
- /research macro, funding, volume, dominance, miner — individual topics
- /research h12_sfp, weekly_sfp, d1_sfps — SFP studies with charts
- /research h12_invalidations, w1_invalidations — invalidation follow-ups
- Add ETH or BTC (default ETH); e.g. /research d1_sfps 5 BTC

When an authoritative cycle snapshot is provided, spot, zones, SFPs, and key levels in your answer
MUST match that snapshot. Do not invent prices or zones that contradict it.

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


def _format_latest_cycle_summary(latest: dict, open_cycle_ids: set[str]) -> str:
    header = "=== Latest hourly cycle"
    if open_cycle_ids and latest.get("cycle_id") not in open_cycle_ids:
        header += " (may differ from open positions)"
    return f"{header} ===\n{_format_suggestion_context(latest)}"


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


def _build_context(spot: float, user_message: str) -> tuple[str, str | None, dict[str, str]]:
    """Return (text context, optional ledger chart path, snapshot marked chart paths)."""
    parts: list[str] = [f"Current ETH spot: ${spot:,.2f}"]
    snapshot_charts: dict[str, str] = {}

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

    position_detail = paper.format_positions_detail(spot)
    chart_path: str | None = None
    open_positions = paper.get_open_positions(spot)
    open_cycle_ids = {
        str(p["open_cycle_id"]) for p in open_positions if p.get("open_cycle_id")
    }
    latest = ledger.get_latest_suggestion()

    if position_detail:
        parts.append("")
        parts.append("=== Open paper positions ===")
        parts.append(position_detail)

        for pos in open_positions:
            cid = pos.get("open_cycle_id")
            if not cid:
                continue
            trade_row = ledger.get_suggestion_by_cycle_id(str(cid))
            if trade_row:
                parts.append("")
                parts.append(f"Rationale for open position (cycle {cid}):")
                parts.append(str(trade_row.get("rationale", "")).strip())
                chart_path = _pick_chart_path(trade_row.get("chart_path"), chart_path)

        if latest:
            parts.append("")
            parts.append(_format_latest_cycle_summary(latest, open_cycle_ids))
            chart_path = _pick_chart_path(latest.get("chart_path"), chart_path)
    elif latest:
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

    closed_detail = paper.format_closed_trades_detail()
    if closed_detail:
        parts.append("")
        parts.append("=== Closed paper trades ===")
        parts.append(closed_detail)

    parts.append("")
    parts.append(paper.format_pnl_footer(spot))
    return "\n".join(parts), chart_path, snapshot_charts


def answer(user_message: str) -> str:
    """Return Claude's reply about the latest suggestion (caller appends PnL footer)."""
    guide = analyze.load_trading_guide()
    spot = research.get_spot_price()

    if ledger.get_latest_suggestion() is None and not paper.is_open():
        return (
            "No trade suggestions yet. The agent runs every hour — check back after the first cycle."
        )

    text_context, chart_path, snapshot_charts = _build_context(spot, user_message)
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
        include_patterns=True,
    )

    user_content: list[dict] = [{"type": "text", "text": text_context}]
    user_content.extend(vision_blocks)

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": guide + "\n\n" + _SYSTEM_SUFFIX,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:
        logger.exception("Chat Claude API call failed")
        return f"Sorry, I could not reach the analysis service right now. ({exc})"

    reply = ""
    for block in response.content:
        if block.type == "text":
            reply += block.text

    return reply.strip()[:3500] if reply.strip() else "I don't have an answer for that right now."
