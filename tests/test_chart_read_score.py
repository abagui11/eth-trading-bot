"""Tests for chart-read score computation."""

import unittest

from critic import AuditFinding, AuditVerdict, compute_chart_read_score


class ChartReadScoreTests(unittest.TestCase):
    def test_clean_verdict_is_100(self) -> None:
        verdict = AuditVerdict(source="hourly")
        score, breakdown = compute_chart_read_score(verdict)
        self.assertEqual(score, 100)
        self.assertEqual(breakdown["critical"], 0)

    def test_critical_and_hallucination_penalties(self) -> None:
        verdict = AuditVerdict(
            source="hourly",
            deterministic=[
                AuditFinding(code="X", message="bad", severity="critical"),
            ],
            llm_hallucinations=[
                AuditFinding(code="LLM_HALLUCINATION", message="wrong level"),
            ],
        )
        score, _ = compute_chart_read_score(verdict)
        self.assertEqual(score, 65)  # 100 - 15 - 20

    def test_sanitized_penalty_floors_at_zero(self) -> None:
        verdict = AuditVerdict(
            source="hourly",
            deterministic=[
                AuditFinding(code="A", message="a", severity="critical"),
                AuditFinding(code="B", message="b", severity="critical"),
                AuditFinding(code="C", message="c", severity="critical"),
                AuditFinding(code="D", message="d", severity="critical"),
                AuditFinding(code="E", message="e", severity="critical"),
            ],
            sanitized=True,
        )
        score, _ = compute_chart_read_score(verdict)
        self.assertEqual(score, 0)


if __name__ == "__main__":
    unittest.main()
