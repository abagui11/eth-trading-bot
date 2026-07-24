"""Asian session grouping and report tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research_reports import catalog, router
from research_reports.topics import asian_session

_ET = ZoneInfo("America/New_York")


def _h1_bar(ts_utc: datetime, open_: float, high: float, low: float, close: float) -> dict:
    return {
        "ts": ts_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


def _asian_session_bars(
    start_et: datetime,
    open_px: float,
    close_px: float,
) -> list[dict]:
    """Build 7 H1 bars for one Asian session starting at 21:00 ET."""
    assert start_et.hour == 21
    bars: list[dict] = []
    # Linear path open → close across 7 hours
    for i in range(7):
        ts = start_et + timedelta(hours=i)
        o = open_px + (close_px - open_px) * i / 7
        c = open_px + (close_px - open_px) * (i + 1) / 7
        hi = max(o, c) + 10
        lo = min(o, c) - 10
        bars.append(_h1_bar(ts, o, hi, lo, c))
    return bars


def test_group_asian_sessions_open_to_close():
    start = datetime(2026, 7, 1, 21, 0, tzinfo=_ET)
    bars = _asian_session_bars(start, open_px=100_000.0, close_px=101_000.0)
    # Add a London-hour bar that must be ignored
    bars.append(
        _h1_bar(datetime(2026, 7, 2, 10, 0, tzinfo=_ET), 101_000, 101_100, 100_900, 101_050)
    )
    sessions = asian_session.group_asian_sessions(bars)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_date.isoformat() == "2026-07-01"
    assert s.open == 100_000.0
    assert abs(s.close - 101_000.0) < 1e-6
    assert abs(s.change - 1_000.0) < 1e-6
    assert s.bar_count == 7


def test_incomplete_session_excluded():
    start = datetime(2026, 7, 1, 21, 0, tzinfo=_ET)
    bars = _asian_session_bars(start, 100_000.0, 101_000.0)[:3]  # missing last hours
    assert asian_session.group_asian_sessions(bars) == []


def test_resolve_asian_session_topic():
    assert router.resolve_topic("/research asian_session") == "asian_session"
    assert router.resolve_topic("/research asia") == "asian_session"
    assert (
        router.resolve_topic(
            "what the net change is during the asian session on BTC price "
            "over last 2 weeks, 4 weeks, and 2 months?"
        )
        == "asian_session"
    )
    assert catalog.is_research_query("asian session BTC net change") is True


def test_catalog_lists_asian_session():
    text = router.build_catalog()
    assert "/research asian_session" in text


def test_build_report_defaults_btc_and_windows():
    start = datetime(2026, 5, 1, 21, 0, tzinfo=_ET)
    bars: list[dict] = []
    # 65 completed sessions with +100 each
    for day in range(65):
        s = start + timedelta(days=day)
        bars.extend(_asian_session_bars(s, 100_000.0 + day, 100_100.0 + day))

    frozen_now = datetime(2026, 7, 10, 12, 0, tzinfo=_ET)  # after 04:00, before 21:00

    with patch("research.fetch_h1_bars", return_value=bars) as mock_fetch:
        report = asian_session.build_asian_session_report(
            product_id="BTC-USD",
            now=frozen_now,
        )

    mock_fetch.assert_called_once()
    assert mock_fetch.call_args.kwargs["product_id"] == "BTC-USD"
    assert report.topic == "asian_session"
    assert "2 weeks" in report.headline
    assert "4 weeks" in report.headline
    assert "2 months" in report.headline
    section_names = [name for name, _ in report.sections]
    assert any("2 weeks" in n for n in section_names)
    assert "21:00–04:00" in report.detail_text or "9pm–4am" in report.detail_text

    # Router default product path
    with patch(
        "research_reports.topics.asian_session.build_asian_session_report",
        return_value=report,
    ) as mock_build:
        out = router.build_report("asian_session", text="/research asian_session")
    mock_build.assert_called_once_with(product_id="BTC-USD")
    assert out.topic == "asian_session"

def test_build_report_honors_eth_product():
    from research_reports.format import ResearchReport

    stub = ResearchReport(
        topic="asian_session",
        title="ETH Asian Session",
        headline="stub",
    )
    with patch(
        "research_reports.topics.asian_session.build_asian_session_report",
        return_value=stub,
    ) as mock_fn:
        router.build_report(
            "asian_session",
            text="/research asian_session ETH",
            product_id="ETH-USD",
        )
    mock_fn.assert_called_once_with(product_id="ETH-USD")
