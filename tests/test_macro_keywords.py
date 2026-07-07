"""Tests for macro keyword relevance scoring."""

from __future__ import annotations

import unittest

from macro.keywords import relevance_score, BLOCKED_STANDALONE


class TestMacroKeywords(unittest.TestCase):
    def test_iran_headline_promotes(self) -> None:
        title = "U.S. revokes Iran oil sales authorization after tanker attacks"
        score, hits = relevance_score(title)
        self.assertGreaterEqual(score, 40)
        terms = {h["term"] for h in hits}
        self.assertTrue(
            "tanker attacks" in terms or "tanker attack" in terms or "iran oil" in terms
        )

    def test_phrase_longest_match_no_double_iran(self) -> None:
        title = "strait of hormuz tensions rise"
        score, hits = relevance_score(title)
        phrase_terms = [h["term"] for h in hits if h["rule"] == "T1_PHRASE"]
        self.assertIn("strait of hormuz", phrase_terms)

    def test_blocked_standalone_fed(self) -> None:
        self.assertIn("fed", BLOCKED_STANDALONE)
        score, hits = relevance_score("FedEx delivers packages faster")
        fed_hits = [h for h in hits if h["term"] == "fed"]
        self.assertEqual(fed_hits, [])

    def test_federal_reserve_promotes(self) -> None:
        score, _ = relevance_score("Federal Reserve holds emergency rate meeting")
        self.assertGreaterEqual(score, 40)

    def test_t3_alone_does_not_promote(self) -> None:
        score, _ = relevance_score("crypto market rally continues")
        self.assertLess(score, 40)

    def test_negative_subtracts(self) -> None:
        score, hits = relevance_score("NBA championship sports betting crypto")
        neg = [h for h in hits if h["rule"].startswith("NEGATIVE")]
        self.assertTrue(neg or score < 15)

    def test_extra_t2_from_config(self) -> None:
        score, hits = relevance_score("fusaka upgrade scheduled", extra_t2=["fusaka"])
        self.assertGreaterEqual(score, 20)
        self.assertTrue(any(h["term"] == "fusaka" for h in hits))


if __name__ == "__main__":
    unittest.main()
