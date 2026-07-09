"""Research report formatting tests."""

from __future__ import annotations

import re

from research_reports.format import ResearchReport, format_report_text

_SECRET_PATTERNS = (
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"ANTHROPIC_API_KEY"),
    re.compile(r"TELEGRAM_BOT_TOKEN"),
)


def _assert_no_secrets(text: str) -> None:
    for pattern in _SECRET_PATTERNS:
        assert not pattern.search(text), f"secret-like value in outbound text: {pattern.pattern}"


def test_format_report_text_structure():
    report = ResearchReport(
        topic="funding",
        title="ETH Funding",
        as_of="2026-07-09T12:00:00Z",
        headline="ETH perp funding +0.0100%",
        sections=[("Metrics", ["• Current rate: +0.0100% per 8h"])],
        interpretation=["Funding near neutral."],
        sources=["Binance Futures API"],
    )
    text = format_report_text(report)
    assert "═══ ETH Research — ETH Funding ═══" in text
    assert "What this means" in text
    assert "Not financial advice." in text
    _assert_no_secrets(text)
