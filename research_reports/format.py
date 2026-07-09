"""Standardized research report format for Telegram delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ResearchReport:
    topic: str
    title: str
    headline: str
    sections: list[tuple[str, list[str]]] = field(default_factory=list)
    interpretation: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    as_of: str = ""
    chart_path: str | None = None
    caption: str | None = None

    def __post_init__(self) -> None:
        if not self.as_of:
            self.as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def detail_text(self) -> str:
        return format_report_text(self)


def format_report_text(report: ResearchReport) -> str:
    """Render a ResearchReport into Telegram-ready text."""
    lines = [
        f"═══ ETH Research — {report.title} ═══",
        f"As of {report.as_of}",
        "",
        report.headline,
        "",
    ]
    for section_name, bullets in report.sections:
        if not bullets:
            continue
        lines.append(section_name)
        lines.extend(bullets)
        lines.append("")

    if report.interpretation:
        lines.append("What this means")
        for item in report.interpretation:
            lines.append(f"• {item}")
        lines.append("")

    if report.sources:
        lines.append(f"Sources: {', '.join(report.sources)}")

    lines.append("Not financial advice.")
    return "\n".join(lines).strip()
