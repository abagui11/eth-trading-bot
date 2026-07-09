"""Research topic routing tests."""

from __future__ import annotations

from research_reports import catalog, router


def test_bare_research_command_returns_no_topic():
    assert catalog.match_topic_from_text("/research") is None


def test_resolve_funding_topic():
    assert router.resolve_topic("/research funding") == "funding"
    assert router.resolve_topic("What's ETH funding right now?") == "funding"


def test_resolve_dominance_topic():
    assert router.resolve_topic("BTC dominance and USDT dominance") == "dominance"


def test_resolve_sfp_studies():
    assert router.resolve_topic("/research h12_sfp") == "h12_sfp"
    assert router.resolve_topic("/research weekly_sfp") == "weekly_sfp"
    assert router.resolve_topic("What % of H12 SFPs reversed in 4 years?") == "h12_sfp"


def test_catalog_lists_snapshot_and_study_topics():
    text = router.build_catalog()
    assert "/research digest" in text
    assert "/research funding" in text
    assert "/research h12_sfp" in text
    assert "Coming soon" in text


def test_coming_soon_topic_builds_placeholder_report():
    report = router.build_report("h12_invalidations")
    assert "not available yet" in report.headline.lower()
