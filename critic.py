"""Monitor agent: deterministic + LLM fact-checking of rationales and chat replies."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic

import analyze
import audit
import config
from models import Suggestion
from patterns.market_context import MarketContext
from patterns.order_block import bounds_close, find_matching_h1_ob, zones_overlap
from patterns.htf_structure import HTFZone

logger = logging.getLogger(__name__)

Severity = Literal["critical", "warning"]
Source = Literal["hourly", "chat"]

_PRICE_RE = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_NEGATION_RE = re.compile(
    r"\b(?:no|not|without|none|lack|missing|absent|didn't|did not|hasn't|has not)\b",
    re.IGNORECASE,
)
_H1_OB_RE = re.compile(
    rf"(?i)H1\s+OB[^0-9]*{_PRICE_RE}\s*[-–]\s*{_PRICE_RE}",
)
_H12_ZONE_RE = re.compile(
    rf"(?i)H12\s+(?:OB|BRKR|breaker|order\s+block)[^0-9]*{_PRICE_RE}\s*[-–]\s*{_PRICE_RE}",
)
_H12_SFP_RE = re.compile(r"(?i)\bH12\s+(?:\w+\s+)?SFP\b")
_H1_SFP_RE = re.compile(r"(?i)\bH1\s+(?:\w+\s+)?SFP\b")
_GENERIC_SFP_RE = re.compile(r"(?i)\bSFP\b")
_KEY_LEVEL_NAMES = (
    "Weekly Open",
    "Daily Open",
    "Monday High",
    "Monday Low",
    "Monday Mid",
    "Prev Week High",
    "Prev Week Low",
    "Prev Week Mid",
    "Monthly Open",
    "Prev Month High",
    "Prev Month Low",
    "Quarterly Open",
    "Yearly Open",
)
_RETEST_NOT_FILLED_RE = re.compile(
    r"(?i)(?:not\s+yet\s+filled|has\s+not\s+reached|waiting\s+for\s+(?:a\s+)?rally|"
    r"hasn't\s+reached|have\s+not\s+reached|not\s+reached\s+the\s+retest)",
)
_RANGE_BREAK_ABOVE_RE = re.compile(r"(?i)(?:broke?\s+above|break\s+above|broken\s+above).*24h")
_RANGE_BREAK_BELOW_RE = re.compile(r"(?i)(?:broke?\s+below|break\s+below|broken\s+below).*24h")


@dataclass
class AuditFinding:
    code: str
    message: str
    severity: Severity = "critical"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


CRITICAL_RETRY_CODES = frozenset({
    "H1_OB_MISLABEL",
    "H1_SFP_NOT_FOUND",
    "H12_SFP_NOT_FOUND",
    "INVALIDATED_SFP_CITED",
    "JSON_H12_AS_H1_OB",
    "RETEST_STATUS_CONFLICT",
    "RANGE_BREAK_CONFLICT",
    "KEY_LEVEL_MISMATCH",
})


@dataclass
class AuditVerdict:
    source: Source
    cycle_id: str | None = None
    user_id: int | None = None
    action: str | None = None
    text_excerpt: str = ""
    deterministic: list[AuditFinding] = field(default_factory=list)
    llm_hallucinations: list[AuditFinding] = field(default_factory=list)
    llm_verified: list[str] = field(default_factory=list)
    sanitized: bool = False

    @property
    def has_issues(self) -> bool:
        return bool(self.deterministic or self.llm_hallucinations)

    def deterministic_dicts(self) -> list[dict[str, str]]:
        return [f.to_dict() for f in self.deterministic]

    def llm_dicts(self) -> list[dict[str, str]]:
        return [f.to_dict() for f in self.llm_hallucinations]


def build_signals_block(alerts: list[str]) -> str | None:
    """Format programmatic alerts for prepending to broadcast rationale."""
    if not alerts:
        return None
    return "Signals: " + " | ".join(alerts)


def split_rationale(full: str) -> tuple[str, str | None]:
    """Split composed rationale into LLM body and optional Signals block."""
    text = full.strip()
    if not text.startswith("Signals:"):
        return text, None
    parts = text.split("\n\n", 1)
    if len(parts) == 1:
        return "", parts[0]
    return parts[1].strip(), parts[0].strip()


def compose_rationale(llm_body: str, signals_block: str | None) -> str:
    """Combine LLM rationale with programmatic Signals block."""
    body = llm_body.strip()
    if not signals_block:
        return body
    if not body:
        return signals_block
    return f"{signals_block}\n\n{body}"


def findings_require_retry(findings: list[AuditFinding]) -> bool:
    """True when deterministic findings warrant one Claude retry."""
    return any(
        f.code in CRITICAL_RETRY_CODES and f.severity == "critical"
        for f in findings
    )


def format_retry_feedback(findings: list[AuditFinding]) -> str:
    """Bullet list of fact-check failures for a Claude retry."""
    critical = [f for f in findings if f.code in CRITICAL_RETRY_CODES]
    if not critical:
        critical = findings
    lines = [f"- {f.code}: {f.message}" for f in critical]
    return "\n".join(lines)


def sanitize_rationale(ctx: MarketContext) -> str:
    """Safe fallback prose built only from programmatic snapshot fields."""
    parts: list[str] = [f"Spot ${ctx.spot:,.2f}."]
    zone_snap = ctx.zone_snapshot
    if zone_snap and zone_snap.primary_bearish:
        z = zone_snap.primary_bearish
        parts.append(
            f"Primary H12 zone: bearish {z.low:,.2f}-{z.high:,.2f} (bias only)."
        )
    elif zone_snap and zone_snap.primary_bullish:
        z = zone_snap.primary_bullish
        parts.append(
            f"Primary H12 zone: bullish {z.low:,.2f}-{z.high:,.2f} (bias only)."
        )
    if ctx.h12_sfps:
        parts.append(
            f"Recent valid H12 SFPs: {len(ctx.h12_sfps)} in snapshot window."
        )
    else:
        parts.append("No valid H12 SFP in the recent window.")
    if ctx.h1_sfps:
        parts.append(
            f"Recent valid H1 SFPs: {len(ctx.h1_sfps)} in snapshot window."
        )
    else:
        parts.append("No valid H1 SFP in the recent window.")
    if ctx.order_blocks:
        ob = ctx.order_blocks[-1]
        parts.append(
            f"Nearest detected H1 OB: {ob.direction} {ob.low:,.2f}-{ob.high:,.2f}."
        )
    else:
        parts.append("No detected H1 OB in lookback — wait for H1 fib retest.")
    parts.append("No trade until LTF structure aligns with programmatic context.")
    return " ".join(parts)


def _parse_price(raw: str) -> float:
    return float(raw.replace(",", ""))


def _price_close(a: float, b: float, tol_pct: float = 0.005) -> bool:
    ref = max(abs(b), 1.0)
    return abs(a - b) / ref <= tol_pct


def _zone_match(
    low: float,
    high: float,
    zones: list[HTFZone],
    *,
    zone_types: set[str] | None = None,
    direction: str | None = None,
) -> HTFZone | None:
    lo, hi = min(low, high), max(low, high)
    for zone in zones:
        if zone_types and zone.zone_type not in zone_types:
            continue
        if direction and zone.direction != direction:
            continue
        if bounds_close(lo, hi, zone.low, zone.high) or zones_overlap(lo, hi, zone.low, zone.high):
            return zone
    return None


def _h1_ob_match(low: float, high: float, ctx: MarketContext) -> bool:
    lo, hi = min(low, high), max(low, high)
    for ob in ctx.order_blocks:
        if bounds_close(lo, hi, ob.low, ob.high):
            return True
    return False


def _mentions_positive_sfp(text: str, timeframe: str) -> bool:
    window_start = 0
    if timeframe == "H12":
        pattern = _H12_SFP_RE
    else:
        pattern = _H1_SFP_RE
    for match in pattern.finditer(text):
        start = match.start()
        prefix = text[max(0, start - 40):start]
        if _NEGATION_RE.search(prefix):
            continue
        return True
    return False


def _mentions_invalidated_sfp(text: str, ctx: MarketContext) -> AuditFinding | None:
    if not ctx.live_invalidated_sfps:
        return None
    if not _GENERIC_SFP_RE.search(text):
        return None
    for event in ctx.live_invalidated_sfps:
        level_str = f"{event.swept_level:,.2f}".replace(".00", "")
        level_plain = f"{event.swept_level:.2f}"
        if level_str in text or level_plain in text or f"{event.swept_level:,.0f}" in text:
            return AuditFinding(
                code="INVALIDATED_SFP_CITED",
                message=(
                    f"Text cites SFP at {event.swept_level:,.2f} ({event.timeframe} "
                    f"{event.direction}) but it was live-invalidated in market context"
                ),
            )
    if _mentions_positive_sfp(text, "H12") and not ctx.h12_sfps:
        for event in ctx.live_invalidated_sfps:
            if event.timeframe == "H12":
                return AuditFinding(
                    code="INVALIDATED_SFP_CITED",
                    message=(
                        f"Text cites H12 SFP but only invalidated H12 SFP exists "
                        f"(@ {event.swept_level:,.2f})"
                    ),
                )
    if _mentions_positive_sfp(text, "H1") and not ctx.h1_sfps:
        for event in ctx.live_invalidated_sfps:
            if event.timeframe == "H1":
                return AuditFinding(
                    code="INVALIDATED_SFP_CITED",
                    message=(
                        f"Text cites H1 SFP but only invalidated H1 SFP exists "
                        f"(@ {event.swept_level:,.2f})"
                    ),
                )
    return None


def _check_h1_ob_bounds(text: str, ctx: MarketContext) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for match in _H1_OB_RE.finditer(text):
        low = _parse_price(match.group(1))
        high = _parse_price(match.group(2))
        if _h1_ob_match(low, high, ctx):
            continue
        h12_match = _zone_match(
            low,
            high,
            ctx.htf_zones,
            zone_types={"order_block", "breaker"},
        )
        if h12_match is not None:
            findings.append(
                AuditFinding(
                    code="H1_OB_MISLABEL",
                    message=(
                        f"H1 OB {low:,.2f}-{high:,.2f} matches H12 "
                        f"{h12_match.zone_type.upper()} {h12_match.low:,.2f}-"
                        f"{h12_match.high:,.2f} — likely H12 zone mislabeled as H1 OB"
                    ),
                )
            )
        else:
            findings.append(
                AuditFinding(
                    code="H1_OB_NOT_FOUND",
                    message=(
                        f"H1 OB {low:,.2f}-{high:,.2f} cited in text but no matching "
                        f"detected H1 order block in snapshot"
                    ),
                    severity="warning",
                )
            )
    return findings


def _check_h12_zone_bounds(text: str, ctx: MarketContext) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for match in _H12_ZONE_RE.finditer(text):
        low = _parse_price(match.group(1))
        high = _parse_price(match.group(2))
        if _zone_match(low, high, ctx.htf_zones):
            continue
        findings.append(
            AuditFinding(
                code="H12_ZONE_NOT_FOUND",
                message=(
                    f"H12 zone {low:,.2f}-{high:,.2f} cited but no matching H12 OB/BRKR "
                    f"in snapshot"
                ),
                severity="warning",
            )
        )
    return findings


def _check_sfp_presence(text: str, ctx: MarketContext) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    if _mentions_positive_sfp(text, "H12") and not ctx.h12_sfps:
        findings.append(
            AuditFinding(
                code="H12_SFP_NOT_FOUND",
                message="Text cites H12 SFP but snapshot has no recent valid H12 SFPs",
            )
        )
    if _mentions_positive_sfp(text, "H1") and not ctx.h1_sfps:
        findings.append(
            AuditFinding(
                code="H1_SFP_NOT_FOUND",
                message="Text cites H1 SFP but snapshot has no recent valid H1 SFPs",
            )
        )
    return findings


def _check_key_levels(text: str, ctx: MarketContext) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    if not ctx.key_levels_near:
        return findings
    for label in _KEY_LEVEL_NAMES:
        pattern = re.compile(
            rf"(?i){re.escape(label)}[^0-9]*{_PRICE_RE}",
        )
        for match in pattern.finditer(text):
            claimed = _parse_price(match.group(1))
            actual = next((lv for lv in ctx.key_levels_near if lv.label == label), None)
            if actual is None:
                actual = next(
                    (lv for lv in ctx.key_levels_near if label.lower() in lv.label.lower()),
                    None,
                )
            if actual is None:
                continue
            if not _price_close(claimed, actual.price):
                findings.append(
                    AuditFinding(
                        code="KEY_LEVEL_MISMATCH",
                        message=(
                            f"{label}: text cites {claimed:,.2f} but snapshot has "
                            f"{actual.price:,.2f}"
                        ),
                    )
                )
    return findings


def _check_retest_status(text: str, ctx: MarketContext) -> AuditFinding | None:
    zone_snap = ctx.zone_snapshot
    if zone_snap is None or zone_snap.bearish_retest_low is None:
        return None
    retest_filled = False
    if ctx.range_24h and ctx.range_24h.high >= zone_snap.bearish_retest_low:
        retest_filled = True
    if not retest_filled:
        return None
    if _RETEST_NOT_FILLED_RE.search(text):
        return AuditFinding(
            code="RETEST_STATUS_CONFLICT",
            message=(
                "Text implies retest not filled / waiting for rally, but snapshot "
                "RETEST STATUS is FILLED (24h high reached supply)"
            ),
        )
    return None


def _check_range_break(text: str, ctx: MarketContext) -> AuditFinding | None:
    if _RANGE_BREAK_ABOVE_RE.search(text) and ctx.range_break != "above":
        return AuditFinding(
            code="RANGE_BREAK_CONFLICT",
            message="Text claims 24h range break above but snapshot range_break is not 'above'",
        )
    if _RANGE_BREAK_BELOW_RE.search(text) and ctx.range_break != "below":
        return AuditFinding(
            code="RANGE_BREAK_CONFLICT",
            message="Text claims 24h range break below but snapshot range_break is not 'below'",
        )
    return None


def _check_rationale_vs_json(text: str, suggestion: Suggestion | None) -> list[AuditFinding]:
    if suggestion is None or suggestion.action == "no_trade":
        return []
    findings: list[AuditFinding] = []
    ob = suggestion.order_block
    if ob and suggestion.entry is not None:
        entry = float(suggestion.entry)
        if f"{entry:,.2f}" not in text and f"{entry:.2f}" not in text:
            findings.append(
                AuditFinding(
                    code="ENTRY_NOT_IN_RATIONALE",
                    message=(
                        f"JSON entry {entry:,.2f} not mentioned in rationale text "
                        f"(warning only)"
                    ),
                    severity="warning",
                )
            )
    if ob:
        low, high = float(ob["low"]), float(ob["high"])
        ob_mentioned = (
            f"{low:,.2f}" in text
            or f"{high:,.2f}" in text
            or f"{low:.2f}" in text
            or f"{high:.2f}" in text
        )
        if not ob_mentioned:
            findings.append(
                AuditFinding(
                    code="ORDER_BLOCK_NOT_IN_RATIONALE",
                    message=(
                        f"JSON order_block {low:,.2f}-{high:,.2f} bounds not cited in rationale"
                    ),
                    severity="warning",
                )
            )
    return findings


def _check_h12_as_order_block_json(ctx: MarketContext, suggestion: Suggestion | None) -> AuditFinding | None:
    if suggestion is None or suggestion.action == "no_trade" or not suggestion.order_block:
        return None
    ob = suggestion.order_block
    direction = "bullish" if suggestion.action in ("spot_buy", "deriv_buy") else "bearish"
    match = find_matching_h1_ob(ob, ctx.order_blocks, direction)  # type: ignore[arg-type]
    if match is not None:
        return None
    h12_match = _zone_match(
        float(ob["low"]),
        float(ob["high"]),
        [z for z in ctx.htf_zones if z.zone_type == "order_block" and not z.mitigated],
        direction=direction,
    )
    if h12_match is not None:
        return AuditFinding(
            code="JSON_H12_AS_H1_OB",
            message=(
                f"order_block JSON {ob['low']}-{ob['high']} matches H12 OB "
                f"({h12_match.low:,.2f}-{h12_match.high:,.2f}) not H1 OB"
            ),
        )
    return None


def verify_deterministic(
    text: str,
    ctx: MarketContext,
    suggestion: Suggestion | None = None,
) -> list[AuditFinding]:
    """Rule-based fact checks against programmatic market context."""
    if not text.strip():
        return []

    findings: list[AuditFinding] = []
    findings.extend(_check_h1_ob_bounds(text, ctx))
    findings.extend(_check_h12_zone_bounds(text, ctx))
    findings.extend(_check_sfp_presence(text, ctx))
    findings.extend(_check_key_levels(text, ctx))
    findings.extend(_check_rationale_vs_json(text, suggestion))

    for checker in (
        lambda: _mentions_invalidated_sfp(text, ctx),
        lambda: _check_retest_status(text, ctx),
        lambda: _check_range_break(text, ctx),
        lambda: _check_h12_as_order_block_json(ctx, suggestion),
    ):
        result = checker()
        if result is not None:
            findings.append(result)

    # Deduplicate by code+message
    seen: set[tuple[str, str]] = set()
    unique: list[AuditFinding] = []
    for finding in findings:
        key = (finding.code, finding.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


_LLM_SYSTEM = """You are a fact-checker for an ETH trading bot. Given authoritative programmatic market context and chart images, verify factual claims in the text under review.

Rules:
- Only flag HALLUCINATION when the text clearly contradicts the market context or visible chart overlays.
- Use UNVERIFIED for subjective or uncheckable claims (trade quality, future price).
- Do NOT evaluate whether the trade is good — only factual accuracy.
- H12 OB/BRKR are HTF zones; H1 OB is separate for entries.
- Only valid SFPs are those listed under Recent H12/H1 SFPs in context (not Live-invalidated).

Return JSON only:
{"claims":[{"claim":"...","verdict":"VERIFIED|UNVERIFIED|HALLUCINATION","reason":"..."}]}
"""


def _extract_llm_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def verify_llm(
    text: str,
    ctx: MarketContext,
    chart_paths: dict[str, str] | None = None,
) -> tuple[list[AuditFinding], list[str]]:
    """Second-pass Claude review for structural / nuanced hallucinations."""
    if not text.strip():
        return [], []

    user_content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Review the following text for factual accuracy against the market context "
                "and charts. Flag only clear HALLUCINATIONs.\n\n"
                f"=== Text under review ===\n{text}\n\n"
                f"=== Authoritative market context ===\n{ctx.summary_text}"
            ),
        },
    ]
    if chart_paths:
        for tf in analyze.CHART_ORDER:
            path = chart_paths.get(tf)
            if path:
                user_content.append({"type": "text", "text": f"--- {tf} marked chart ---"})
                user_content.append(analyze._image_block(path))

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=[{"type": "text", "text": _LLM_SYSTEM}],
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:
        logger.exception("LLM critic call failed")
        return [
            AuditFinding(
                code="LLM_CRITIC_ERROR",
                message=f"LLM critic unavailable: {exc}",
                severity="warning",
            )
        ], []

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw += block.text

    try:
        data = _extract_llm_json(raw)
    except json.JSONDecodeError:
        logger.warning("LLM critic returned non-JSON: %s", raw[:300])
        return [], []

    findings: list[AuditFinding] = []
    verified: list[str] = []
    for item in data.get("claims", []):
        verdict = str(item.get("verdict", "")).upper()
        claim = str(item.get("claim", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if verdict == "HALLUCINATION":
            findings.append(
                AuditFinding(
                    code="LLM_HALLUCINATION",
                    message=f"{claim} — {reason}" if reason else claim,
                )
            )
        elif verdict == "VERIFIED" and claim:
            verified.append(claim if not reason else f"{claim} ({reason})")

    return findings, verified[:6]


def _text_excerpt(text: str, limit: int = 280) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "..."


def audit_text(
    text: str,
    ctx: MarketContext,
    *,
    source: Source,
    cycle_id: str | None = None,
    user_id: int | None = None,
    suggestion: Suggestion | None = None,
    chart_paths: dict[str, str] | None = None,
    run_llm: bool = True,
    sanitized: bool = False,
) -> AuditVerdict:
    """Run deterministic checks and optional LLM critic; persist verdict."""
    deterministic = verify_deterministic(text, ctx, suggestion=suggestion)
    llm_hallucinations: list[AuditFinding] = []
    llm_verified: list[str] = []
    if run_llm and (deterministic or text.strip()):
        llm_hallucinations, llm_verified = verify_llm(text, ctx, chart_paths=chart_paths)
        llm_hallucinations = [f for f in llm_hallucinations if f.code == "LLM_HALLUCINATION"]

    verdict = AuditVerdict(
        source=source,
        cycle_id=cycle_id,
        user_id=user_id,
        action=suggestion.action if suggestion else None,
        text_excerpt=_text_excerpt(text),
        deterministic=deterministic,
        llm_hallucinations=llm_hallucinations,
        llm_verified=llm_verified,
        sanitized=sanitized,
    )

    audit.save_verdict(
        source=source,
        cycle_id=cycle_id,
        user_id=user_id,
        deterministic_findings=verdict.deterministic_dicts(),
        llm_findings=verdict.llm_dicts(),
        has_issues=verdict.has_issues,
    )
    return verdict


def audit_hourly_cycle(
    cycle_id: str,
    suggestion: Suggestion,
    market_context: MarketContext,
    marked_chart_paths: dict[str, str],
    *,
    llm_rationale: str | None = None,
    run_llm: bool = True,
    sanitized: bool = False,
) -> AuditVerdict:
    """Audit hourly suggestion rationale after snapshot is saved."""
    text = llm_rationale if llm_rationale is not None else split_rationale(suggestion.rationale)[0]
    return audit_text(
        text,
        market_context,
        source="hourly",
        cycle_id=cycle_id,
        suggestion=suggestion,
        chart_paths=marked_chart_paths,
        run_llm=run_llm,
        sanitized=sanitized,
    )


def audit_chat_reply(
    user_id: int,
    question: str,
    reply: str,
    *,
    cycle_id: str | None = None,
) -> AuditVerdict:
    """Audit a chat bot reply against the best available snapshot."""
    snapshot_row = audit.get_snapshot(cycle_id) if cycle_id else audit.get_latest_snapshot()
    if snapshot_row is None:
        logger.warning("No audit snapshot for chat audit (cycle_id=%s)", cycle_id)
        return AuditVerdict(source="chat", user_id=user_id, text_excerpt=_text_excerpt(reply))

    ctx = audit.market_context_from_dict(snapshot_row["snapshot"])
    suggestion = audit.suggestion_from_dict(snapshot_row["suggestion"])
    chart_paths = snapshot_row.get("marked_chart_paths") or {}
    resolved_cycle = cycle_id or snapshot_row.get("cycle_id")

    verdict = audit_text(
        reply,
        ctx,
        source="chat",
        cycle_id=resolved_cycle,
        user_id=user_id,
        suggestion=suggestion,
        chart_paths=chart_paths,
        run_llm=True,
    )
    audit.log_chat_audit(
        user_id,
        question,
        reply,
        cycle_id=resolved_cycle,
    )
    return verdict
