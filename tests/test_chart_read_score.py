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

    def test_downgrade_penalty_floors_at_zero(self) -> None:
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
            downgraded=True,
        )
        score, _ = compute_chart_read_score(verdict)
        self.assertEqual(score, 0)  # 100 - 75 - 30, clamped

    def test_downgrade_penalised_harder_than_prose_sanitize(self) -> None:
        """A killed trade and a rewritten abstention are not the same event."""
        prose_only = AuditVerdict(source="hourly", sanitized=True)
        killed_trade = AuditVerdict(source="hourly", sanitized=True, downgraded=True)
        self.assertEqual(compute_chart_read_score(prose_only)[0], 90)
        self.assertEqual(compute_chart_read_score(killed_trade)[0], 70)

    def test_sanitize_reasons_survive_into_breakdown(self) -> None:
        """The audit re-verifies the replacement prose, so without this the
        reason a cycle was sanitized is unrecoverable."""
        verdict = AuditVerdict(
            source="hourly",
            sanitized=True,
            sanitize_reasons=["CONTEXT_CONFLICT_UNACKNOWLEDGED"],
        )
        _, breakdown = compute_chart_read_score(verdict)
        self.assertEqual(
            breakdown["sanitize_reasons"], ["CONTEXT_CONFLICT_UNACKNOWLEDGED"]
        )


if __name__ == "__main__":
    unittest.main()
