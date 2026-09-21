"""Eva Brain — Telegram + dashboard copy for Today's Read / ICT / Cycle / News.

Assembles artifacts the system already produces. Today's Read may make one
fast-model call to compress ICT + cycle + news into ≤3 Telegram paragraphs;
everything else is deterministic formatting so Charts / ICT / Cycle / News
stay instant and free.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_NEWS = 4
_VISION_PRODUCTS = ("BTC-USD", "ETH-USD")
_VISION_TFS = ("H4", "H1", "M15")
_TF_ORDER = {"H4": 0, "H1": 1, "M15": 2, "M5": 3}

REFRESH_NOTE = (
    "Charts and the ICT read refresh with every cycle (about every 30 minutes)."
)

# Cache Today's Read so dashboard polling / repeated taps don't re-bill Haiku.
_SYNTH_CACHE: dict[str, tuple[float, str]] = {}
_SYNTH_TTL_SEC = 25 * 60  # slightly under the ~30m cycle


def _fmt_px(value: Any, *, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "?"


def _fmt_zone(lo: Any, hi: Any) -> str:
    try:
        lo_f, hi_f = float(lo), float(hi)
    except (TypeError, ValueError):
        return "?"
    if abs(lo_f - hi_f) < 1e-9:
        return _fmt_px(lo_f)
    return f"{_fmt_px(lo_f)}–{_fmt_px(hi_f)}"


def _kind_label(kind: Any) -> str:
    return str(kind or "").replace("_", " ").strip() or "level"


def _product_label(product_id: Any) -> str:
    return str(product_id or "?").replace("-USD", "")


def _sort_reads(reads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(r: dict[str, Any]) -> tuple:
        pid = str(r.get("product_id") or "")
        tf = str(r.get("timeframe") or "").upper()
        prod_rank = 0 if pid.startswith("BTC") else (1 if pid.startswith("ETH") else 2)
        return (prod_rank, _TF_ORDER.get(tf, 9), tf)

    return sorted(reads, key=key)


def _latest_reads() -> list[dict[str, Any]]:
    try:
        from intelligence import store

        return _sort_reads(list(store.latest_reads()))
    except Exception:  # noqa: BLE001
        logger.exception("brain: conditional reads unavailable")
        return []


def _holds_cell(read: dict[str, Any]) -> str:
    if not read.get("repelling_kind"):
        return "—"
    kind = _kind_label(read.get("repelling_kind"))
    # Keep detector tags like fvg@H4 / breaker@H4 intact in the kind string.
    raw_kind = str(read.get("repelling_kind") or "")
    if "@" in raw_kind:
        kind = raw_kind.replace("_", " ")
    zone = _fmt_zone(read.get("repelling_lo"), read.get("repelling_hi"))
    state = str(read.get("repelling_state") or "").replace("_", " ").strip()
    cell = f"{kind} {zone}"
    if state:
        cell += f" {state}"
    return cell


def _draw_cell(read: dict[str, Any]) -> str:
    if not read.get("attracting_kind"):
        return "—"
    raw_kind = str(read.get("attracting_kind") or "")
    kind = raw_kind.replace("_", " ") if "@" in raw_kind else _kind_label(raw_kind)
    lo = read.get("attracting_lo")
    hi = read.get("attracting_hi")
    try:
        if lo is not None and hi is not None and abs(float(lo) - float(hi)) > 1e-9:
            px = _fmt_zone(lo, hi)
        else:
            px = _fmt_px(lo if lo is not None else hi)
    except (TypeError, ValueError):
        px = "?"
    return f"{kind} {px}"


def _inv_cell(read: dict[str, Any]) -> str:
    inv = read.get("invalidation_price")
    if inv is None:
        return "—"
    cell = _fmt_px(inv)
    if read.get("stale_invalidation"):
        cell += " stale"
    return cell


def _bias_cell(read: dict[str, Any]) -> str:
    bias = read.get("bias")
    if not bias:
        return "no call"
    return str(bias).replace("_", " ")


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def vision_chart_paths() -> list[str]:
    """Marked stance-board PNGs (BTC/ETH × H4/H1/M15), order preserved."""
    try:
        from dashboard.charts import stance_chart_path
    except Exception:  # noqa: BLE001
        logger.exception("brain: stance chart resolver unavailable")
        return []
    paths: list[str] = []
    for product_id in _VISION_PRODUCTS:
        for tf in _VISION_TFS:
            path = stance_chart_path(product_id, tf)
            if path is not None and path.is_file():
                paths.append(str(path))
    return paths


def vision_charts_caption() -> str:
    return (
        "Vision across the timeframes Eva is monitoring — H4 / H1 / M15 for "
        "BTC and ETH, with relevant order blocks and key levels marked.\n"
        "No buy/sell recommendation on these boards.\n\n"
        + REFRESH_NOTE
    )


def cycle_chart_path() -> str | None:
    try:
        from intelligence import store

        row = store.latest_long_thesis()
    except Exception:  # noqa: BLE001
        logger.exception("brain: cycle chart unavailable")
        return None
    if not row:
        return None
    raw = row.get("chart_path")
    if not raw:
        return None
    path = Path(str(raw))
    return str(path) if path.is_file() else None


# ---------------------------------------------------------------------------
# ICT table (dashboard columns, all published timeframes)
# ---------------------------------------------------------------------------

def ict_table_rows() -> list[dict[str, Any]]:
    """Rows shaped like the dashboard ICT table, one per product/timeframe."""
    rows: list[dict[str, Any]] = []
    for read in _latest_reads():
        rows.append(
            {
                "product_id": read.get("product_id"),
                "timeframe": str(read.get("timeframe") or "").upper(),
                "label": (
                    f"{_product_label(read.get('product_id'))} "
                    f"{str(read.get('timeframe') or '').upper()}"
                ),
                "bias": _bias_cell(read),
                "holds": _holds_cell(read),
                "drawing": _draw_cell(read),
                "invalid": _inv_cell(read),
                "location": str(read.get("location") or "—").upper()
                if read.get("location")
                else "—",
                "rationale": str(read.get("rationale") or "").strip(),
                "dropped_reason": str(read.get("dropped_reason") or "").strip(),
                "raw": read,
            }
        )
    return rows


def _ict_waiting_paragraph(rows: list[dict[str, Any]]) -> str:
    """Plain-English: what the table means and what price must do next."""
    if not rows:
        return (
            "No ICT conditional rows this cycle — detectors did not publish a "
            "named order block, breaker, or fair-value gap to watch. The next "
            "cycle (~30 minutes) will try again."
        )

    untested = sum(
        1
        for r in rows
        if "untested" in str(r.get("holds") or "").lower()
    )
    no_call = sum(1 for r in rows if r.get("bias") == "no call")
    with_bias = [r for r in rows if r.get("bias") not in ("no call", "—", "")]

    bits: list[str] = [
        "How to read this: Bias only exists while the named level in "
        "“While this holds” is intact and price is drawing toward the level in "
        "“Drawing to.” Invalidation is usually an M5 close through the level. "
        "Location is premium / discount / equilibrium in the active range."
    ]

    if with_bias:
        parts = [
            f"{r['label']} leans {r['bias']} while {r['holds']} holds, "
            f"drawing to {r['drawing']}"
            for r in with_bias[:3]
        ]
        bits.append("Active lean: " + "; ".join(parts) + ".")
    elif no_call:
        bits.append(
            f"Right now {no_call} of {len(rows)} rows are “no call” — Eva has "
            "named the arrays but is not locking a directional bias until price "
            "trades into them or a holding state confirms."
        )

    if untested:
        bits.append(
            f"Waiting on price: {untested} array(s) still untested — the setup "
            "arms if price revisits those zones; it dies if invalidation prints "
            "first."
        )

    bits.append(REFRESH_NOTE)
    return " ".join(bits)


def ict_view_text() -> str:
    """Monospace ICT table for Telegram + a short waiting-for paragraph."""
    rows = ict_table_rows()
    if not rows:
        return (
            "ICT table\n\n"
            "No conditional read this cycle for any timeframe.\n\n"
            + _ict_waiting_paragraph(rows)
        )

    # Compact pipe table — Telegram <pre> keeps columns aligned.
    headers = ("TF", "Bias", "Holds", "Draw", "Inv", "Loc")
    body: list[list[str]] = []
    for r in rows:
        body.append(
            [
                r["label"][:8],
                str(r["bias"])[:8],
                str(r["holds"])[:28],
                str(r["drawing"])[:22],
                str(r["invalid"])[:10],
                str(r["location"])[:8],
            ]
        )

    widths = [len(h) for h in headers]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    lines = [
        "ICT table — all published timeframes",
        "",
        fmt(list(headers)),
        "-+-".join("-" * w for w in widths),
    ]
    for row in body:
        lines.append(fmt(row))

    # Detail block under the grid so nothing important is truncated away.
    lines += ["", "Detail:"]
    for r in rows:
        lines.append(
            f"• {r['label']}: bias {r['bias']} | holds {r['holds']} | "
            f"draw {r['drawing']} | inv {r['invalid']} | {r['location']}"
        )
        if r.get("rationale"):
            lines.append(f"  {r['rationale'][:200]}")

    lines += ["", _ict_waiting_paragraph(rows)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------

def cycle_text() -> str:
    """Four-year cycle with clearer spacing for Telegram."""
    try:
        from intelligence import cycle_phases, store

        phase, days_since = cycle_phases.current_phase()
        label = cycle_phases.PHASE_LABELS.get(phase, phase.replace("_", " "))

        lines = [
            "Four-year cycle",
            "",
            f"Phase: {label}",
            f"Since last halving: day {days_since}",
        ]

        thesis_row = store.latest_long_thesis()
        if not thesis_row:
            return "\n".join(lines)

        thesis = thesis_row.get("thesis") or {}
        pos = thesis.get("cycle_position") or {}

        months = pos.get("months_since_halving")
        progress = pos.get("cycle_progress_pct")
        dd = pos.get("drawdown_from_ath_pct")
        if months is not None:
            lines.append(f"Clock: {months}m · {days_since}d into the post-halving regime")
        if progress is not None:
            lines.append(f"Cycle progress: {progress}%")
        if dd is not None:
            lines.append(f"From ATH: {dd:+.2f}%")

        bias = str(thesis.get("bias") or "").strip()
        conf = thesis.get("confidence")
        if bias:
            head = f"Thesis: {bias}"
            if conf is not None:
                try:
                    head += f" ({float(conf):.0%} confidence)"
                except (TypeError, ValueError):
                    pass
            lines += ["", head]

        pivot = pos.get("next_projected_pivot") or {}
        days_to = pos.get("days_to_next_pivot")
        if pivot:
            kind = str(pivot.get("kind") or "pivot")
            date = str(pivot.get("date") or "")
            lines.append("")
            pivot_line = f"Next projected pivot: {kind}"
            if date:
                pivot_line += f" · {date}"
            if days_to is not None:
                pivot_line += f" ({days_to}d away)"
            lines.append(pivot_line)
            if str(kind).lower() == "low":
                if days_to is not None:
                    lines.append(
                        f"Eva's read on the bottom: a projected cycle low in "
                        f"about {days_to} day(s)."
                    )
                else:
                    lines.append(
                        "Eva's read on the bottom: next projected pivot is a "
                        "cycle low."
                    )

        summary = str(
            thesis.get("summary") or thesis.get("btc_thesis") or ""
        ).strip()
        if summary:
            lines += ["", summary[:500]]

        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        logger.exception("brain: cycle overview unavailable")
        return "Four-year cycle: read unavailable right now."


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

def _news_events() -> list[dict[str, Any]]:
    try:
        from macro import store as macro_store

        events = macro_store.get_active_events(min_severity=3)[:_MAX_NEWS]
        if not events:
            events = macro_store.get_active_events(min_severity=1)[:_MAX_NEWS]
        return events
    except Exception:  # noqa: BLE001
        logger.exception("brain: macro events unavailable")
        return []


def _news_bias(event: dict[str, Any]) -> dict[str, Any]:
    try:
        from macro.bias_score import best_bias

        return best_bias(event)
    except Exception:  # noqa: BLE001
        return {
            "side": event.get("eth_bias") or event.get("bias_side"),
            "pct": event.get("bias_pct"),
            "one_liner": event.get("eth_impact_summary"),
        }


def news_text() -> str:
    """Telegram news cards: bias% | severity/5 | link, then headline + read."""
    events = _news_events()
    if not events:
        return (
            "News\n\n"
            "Nothing severe is live right now — no active headline is gating "
            "entries."
        )

    lines = [
        "News",
        "Severity is on a scale of 1 to 5.",
        "",
    ]
    for e in events:
        bias = _news_bias(e)
        side = str(bias.get("side") or "neutral").replace("_", " ").strip()
        pct = bias.get("pct")
        sev = e.get("severity")
        title = str(e.get("title") or e.get("headline") or "").strip()
        url = str(e.get("url") or "").strip()
        read = str(
            bias.get("one_liner")
            or e.get("bias_one_liner")
            or e.get("eth_impact_summary")
            or ""
        ).strip()

        pct_s = f"{int(pct)}%" if pct is not None else "—"
        sev_s = f"severity {sev}/5" if sev is not None else "severity —/5"
        link_s = url if url else "(no link)"

        lines.append(f"{side} {pct_s} | {sev_s} | {link_s}")
        lines.append(title or "(untitled)")
        if read:
            lines.append(read[:240])
        lines.append("")

    return "\n".join(lines).rstrip()


def news_html() -> str:
    """HTML version with clickable links for Telegram parse_mode=HTML."""
    import html as html_mod

    events = _news_events()
    if not events:
        return html_mod.escape(news_text())

    parts = [
        "<b>News</b>",
        "Severity is on a scale of 1 to 5.",
        "",
    ]
    for e in events:
        bias = _news_bias(e)
        side = str(bias.get("side") or "neutral").replace("_", " ").strip()
        pct = bias.get("pct")
        sev = e.get("severity")
        title = str(e.get("title") or e.get("headline") or "").strip()
        url = str(e.get("url") or "").strip()
        read = str(
            bias.get("one_liner")
            or e.get("bias_one_liner")
            or e.get("eth_impact_summary")
            or ""
        ).strip()

        pct_s = f"{int(pct)}%" if pct is not None else "—"
        sev_s = f"severity {sev}/5" if sev is not None else "severity —/5"
        meta = html_mod.escape(f"{side} {pct_s} | {sev_s}")
        if url:
            link = f'<a href="{html_mod.escape(url, quote=True)}">link</a>'
            parts.append(f"{meta} | {link}")
        else:
            parts.append(f"{meta} | (no link)")
        parts.append(f"<b>{html_mod.escape(title or '(untitled)')}</b>")
        if read:
            parts.append(html_mod.escape(read[:240]))
        parts.append("")
    return "\n".join(parts).rstrip()


# ---------------------------------------------------------------------------
# Today's Read (≤3 paragraphs, LLM with deterministic fallback)
# ---------------------------------------------------------------------------

def _synth_facts() -> dict[str, Any]:
    """Compact structured facts for the synthesizer / cache key."""
    rows = ict_table_rows()
    ict_lines = [
        f"{r['label']}: bias={r['bias']}; holds={r['holds']}; "
        f"draw={r['drawing']}; inv={r['invalid']}; loc={r['location']}"
        for r in rows
    ]
    cycle = cycle_text()
    news_bits: list[str] = []
    for e in _news_events():
        bias = _news_bias(e)
        news_bits.append(
            f"{bias.get('side')} {bias.get('pct')}% sev={e.get('severity')} "
            f"| {e.get('title')} | "
            f"{bias.get('one_liner') or e.get('eth_impact_summary') or ''}"
        )
    return {
        "ict": ict_lines,
        "cycle": cycle,
        "news": news_bits,
    }


def _synth_cache_key(facts: dict[str, Any]) -> str:
    return repr(
        (facts.get("ict"), facts.get("cycle"), facts.get("news"))
    )[:2000]


def _deterministic_synthesis(facts: dict[str, Any]) -> str:
    """Fallback when the LLM is down — still ≤3 short paragraphs."""
    cycle = str(facts.get("cycle") or "").strip()
    cycle_lines = [ln for ln in cycle.splitlines() if ln.strip()]
    cycle_head = " ".join(cycle_lines[:4]) if cycle_lines else (
        "Cycle read unavailable."
    )

    ict = facts.get("ict") or []
    if not ict:
        ict_para = (
            "Near-term ICT: no conditional arrays published this cycle."
        )
    else:
        ict_para = (
            "Near-term ICT: "
            + " ".join(str(x) for x in ict[:4])
        )
        if len(ict_para) > 420:
            ict_para = ict_para[:417].rstrip() + "…"

    news = facts.get("news") or []
    if not news:
        news_para = (
            "Macro: no severe headline is gating entries right now. "
            + REFRESH_NOTE
        )
    else:
        news_para = (
            "Macro: "
            + " · ".join(str(x)[:120] for x in news[:2])
            + ". "
            + REFRESH_NOTE
        )

    return "\n\n".join([cycle_head, ict_para, news_para])


def _llm_synthesis(facts: dict[str, Any]) -> str | None:
    """One Haiku call → 1–3 Telegram paragraphs."""
    try:
        import anthropic

        import config
        from analyze import log_anthropic_usage
    except Exception:  # noqa: BLE001
        return None
    if not getattr(config, "ANTHROPIC_API_KEY", None):
        return None

    user = (
        "Write Eva's Today's Read for a Telegram chat.\n"
        "Rules:\n"
        "- Exactly 1 to 3 short paragraphs (blank line between them).\n"
        "- Plain text only. No markdown, no bullets, no tables, no headings.\n"
        "- Synthesize the ICT conditional read, the four-year cycle, and the "
        "news/macro into one coherent market view.\n"
        "- Say what Eva is waiting for price to do when ICT rows are no-call "
        "or untested.\n"
        "- End the last paragraph with a brief reminder that charts and the "
        "ICT read refresh about every 30 minutes.\n"
        "- Max ~120 words total.\n\n"
        f"CYCLE:\n{facts.get('cycle')}\n\n"
        f"ICT ROWS:\n" + ("\n".join(facts.get("ict") or ["(none)"]) + "\n\n")
        + "NEWS:\n" + ("\n".join(facts.get("news") or ["(none)"]))
    )
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL_FAST,
            max_tokens=280,
            system=(
                "You are Eva, a crypto market brain. Write tight Telegram copy. "
                "Never invent levels that are not in the facts."
            ),
            messages=[{"role": "user", "content": user}],
        )
        log_anthropic_usage(response, "brain_todays_read")
    except Exception:  # noqa: BLE001
        logger.exception("brain: Today's Read LLM failed")
        return None

    raw = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            raw += block.text
    text = raw.strip()
    if not text:
        return None
    # Hard-cap runaway replies.
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paras) > 3:
        paras = paras[:3]
    return "\n\n".join(paras)


def synthesize_read(*, use_llm: bool = True) -> str:
    """≤3-paragraph Today's Read for Telegram and the dashboard."""
    facts = _synth_facts()
    key = _synth_cache_key(facts)
    now = time.time()
    cached = _SYNTH_CACHE.get(key)
    if cached and now - cached[0] < _SYNTH_TTL_SEC:
        return cached[1]

    text = None
    if use_llm:
        text = _llm_synthesis(facts)
    if not text:
        text = _deterministic_synthesis(facts)

    _SYNTH_CACHE.clear()
    _SYNTH_CACHE[key] = (now, text)
    return text


def build_report() -> dict[str, Any]:
    """/brain command payload: synthesis text + vision charts."""
    text = (
        "Today's Read\n\n"
        + synthesize_read()
    )
    paths = vision_chart_paths()
    return {
        "text": text,
        "chart_paths": paths,
        "caption": vision_charts_caption() if paths else None,
        "view": None,
    }
