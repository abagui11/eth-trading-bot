"""Tests for Telegram notify formatting and broadcast policy helpers."""

from __future__ import annotations

from critic import AuditFinding, AuditVerdict
from notify import format_hourly_monitor_report


def test_hourly_monitor_report_no_trade_skipped_broadcast():
    verdict = AuditVerdict(
        source="hourly",
        cycle_id="20260701T120000Z",
        action="no_trade",
        text_excerpt="HTF bearish, no valid entry.",
        deterministic=[],
        llm_hallucinations=[],
        llm_verified=["Bearish H12 structure cited correctly"],
    )
    text = format_hourly_monitor_report(verdict, broadcast_sent=False)
    assert "NO_TRADE" in text
    assert "Subscriber broadcast: skipped (no_trade)" in text
    assert "All deterministic fact-checks passed" in text
    assert "VERIFIED CLAIMS" in text


def test_hourly_monitor_report_trade_with_issues():
    verdict = AuditVerdict(
        source="hourly",
        cycle_id="20260701T130000Z",
        action="deriv_sell",
        text_excerpt="Short at H1 OB retest.",
        deterministic=[
            AuditFinding(code="H1_OB_MISLABEL", message="bounds wrong"),
        ],
        llm_hallucinations=[
            AuditFinding(code="LLM_HALLUCINATION", message="fake SFP"),
        ],
        sanitized=True,
    )
    text = format_hourly_monitor_report(verdict, broadcast_sent=True)
    assert "Subscriber broadcast: sent" in text
    assert "H1_OB_MISLABEL" in text
    assert "LLM_HALLUCINATION" in text
    assert "sanitized" in text.lower()


def test_hourly_monitor_report_shows_refine_metadata():
    verdict = AuditVerdict(
        source="hourly",
        cycle_id="20260701T140000Z",
        action="no_trade",
        text_excerpt="Audit downgrade.",
        deterministic=[],
        llm_hallucinations=[],
        sanitized=True,
        downgraded=True,
        passes_used=2,
    )
    text = format_hourly_monitor_report(verdict, broadcast_sent=False)
    assert "downgraded to no_trade" in text
    assert "Refine passes used: 2" in text
