"""Research topic routing tests."""

from __future__ import annotations

from unittest.mock import patch

from research_reports import catalog, router
from research_reports.format import ResearchReport


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
    assert router.resolve_topic("/research d1_sfps") == "d1_sfps"
    assert router.resolve_topic("What % of H12 SFPs reversed in 4 years?") == "h12_sfp"
    assert router.resolve_topic("how many daily SFPs in 5 years") == "d1_sfps"


def test_catalog_lists_snapshot_and_study_topics():
    text = router.build_catalog()
    assert "/research digest" in text
    assert "/research funding" in text
    assert "/research h12_sfp" in text
    assert "/research d1_sfps" in text
    assert "/research h12_invalidations" in text
    assert "/research w1_invalidations" in text


def test_resolve_h12_invalidations_topic():
    assert router.resolve_topic("/research h12_invalidations") == "h12_invalidations"
    assert router.resolve_topic("last 10 times an H12 SFP was invalidated") == "h12_invalidations"


def test_resolve_w1_invalidations_topic():
    assert router.resolve_topic("/research w1_invalidations") == "w1_invalidations"
    assert router.resolve_topic("weekly sfp invalidated") == "w1_invalidations"


def test_catalog_lists_h12_invalidations_study():
    text = router.build_catalog()
    assert "/research h12_invalidations" in text
    assert "Coming soon" not in text


def test_parse_product_id():
    assert router.parse_product_id("/research d1_sfps 5 BTC") == "BTC-USD"
    assert router.parse_product_id("/research d1_sfps 5 ETH") == "ETH-USD"
    assert router.parse_product_id("/research d1_sfps 5") == "ETH-USD"


def test_clarify_ambiguous_and_refuse_unindexed():
    assert router.clarify_or_refuse("how many SFPs in the past 5 years") is not None
    assert "Which SFP study" in router.clarify_or_refuse("how many SFPs in the past 5 years")
    refuse = router.clarify_or_refuse("how many M5 OBs in 2019")
    assert refuse is not None
    assert "not indexed" in refuse.lower()


def test_h12_invalidations_builds_study_with_limit():
    stub = ResearchReport(
        topic="h12_invalidations",
        title="H12 Invalidation Study",
        headline="stub",
    )
    with patch("analytics.h12_invalidations_report", return_value=stub) as mock_fn:
        report = router.build_report("h12_invalidations", years=3, text="last 5 invalidations")
    mock_fn.assert_called_once_with(3, limit=5, product_id="ETH-USD")
    assert report.topic == "h12_invalidations"


def test_d1_sfps_builds_with_btc_product():
    stub = ResearchReport(topic="d1_sfps", title="Daily SFP Study", headline="stub")
    with patch("analytics.daily_sfp_report", return_value=stub) as mock_fn:
        report = router.build_report(
            "d1_sfps",
            years=5,
            text="/research d1_sfps 5 BTC",
            product_id="BTC-USD",
        )
    mock_fn.assert_called_once_with(5, product_id="BTC-USD")
    assert report.topic == "d1_sfps"
