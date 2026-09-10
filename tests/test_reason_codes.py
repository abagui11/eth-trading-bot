"""Per-product decision selection and the ledger's reason_code vocabulary."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import agent
import config
import ledger
from models import Suggestion


def _trade(product_id: str) -> Suggestion:
    return Suggestion(
        action="spot_buy",
        size=1000.0,
        entry=2400.0,
        stop_loss=2350.0,
        take_profits=[2500.0],
        risk_reward=2.0,
        rationale="M5 OB fib entry",
        product_id=product_id,
    )


class SelectDecisionsTests(unittest.TestCase):
    """`agent.select_decisions` must not discard a product's real verdict."""

    def test_actionable_product_does_not_evict_the_other_verdict(self) -> None:
        """The bug: filtering to actionable dropped BTC's considered abstention.

        BTC's row was then backfilled with generic filler, so a real
        "no setup because X" looked identical to never having been read.
        """
        btc = Suggestion.no_trade(
            "BTC is mid-range with no M5 order block.",
            product_id="BTC-USD",
            reason_code="model_no_trade",
        )
        selected = agent.select_decisions([_trade("ETH-USD"), btc])

        by_product = {s.product_id: s for s in selected}
        self.assertEqual(len(selected), 2)
        self.assertEqual(by_product["ETH-USD"].action, "spot_buy")
        self.assertEqual(
            by_product["BTC-USD"].rationale,
            "BTC is mid-range with no M5 order block.",
        )
        self.assertEqual(by_product["BTC-USD"].reason_code, "model_no_trade")

    def test_all_abstaining_keeps_every_product_verdict(self) -> None:
        eth = Suggestion.no_trade("ETH ranging.", product_id="ETH-USD")
        btc = Suggestion.no_trade("BTC ranging.", product_id="BTC-USD")
        selected = agent.select_decisions([eth, btc])

        rationales = {s.product_id: s.rationale for s in selected}
        self.assertEqual(rationales["ETH-USD"], "ETH ranging.")
        self.assertEqual(rationales["BTC-USD"], "BTC ranging.")

    def test_unanswered_product_is_marked_not_evaluated(self) -> None:
        selected = agent.select_decisions([_trade("ETH-USD")])
        by_product = {s.product_id: s for s in selected}

        btc = by_product["BTC-USD"]
        self.assertEqual(btc.action, "no_trade")
        self.assertEqual(btc.reason_code, "not_evaluated")
        self.assertIn("No independent setup", btc.rationale)

    def test_empty_proposal_is_marked_proposal_empty(self) -> None:
        selected = agent.select_decisions([])
        codes = {s.product_id: s.reason_code for s in selected}
        self.assertEqual(codes["ETH-USD"], "proposal_empty")
        self.assertEqual(codes["BTC-USD"], "not_evaluated")


class ClassifyReasonTests(unittest.TestCase):
    def test_explicit_code_wins(self) -> None:
        s = Suggestion.no_trade("whatever", reason_code="not_evaluated")
        self.assertEqual(ledger.classify_reason(s), "not_evaluated")

    def test_actionable_defaults_to_trade(self) -> None:
        self.assertEqual(ledger.classify_reason(_trade("ETH-USD")), "trade")

    def test_prose_prefixes_are_recognised_for_legacy_callers(self) -> None:
        cases = {
            "parse_error: recomputed R/R 0.83 below 1.0 minimum": "validation_rejected",
            "api_error: connection reset": "proposal_error",
            "Audit downgrade (M5_OB_MISLABEL): ...": "audit_downgrade",
            "Price is mid-range; waiting for a retest.": "model_no_trade",
        }
        for rationale, expected in cases.items():
            with self.subTest(rationale=rationale):
                s = Suggestion.no_trade(rationale)
                self.assertEqual(ledger.classify_reason(s), expected)


class LedgerReasonCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = patch.object(config, "LEDGER_DB", self._tmpdir.name + "/ledger.db")
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmpdir.cleanup()

    def _append(self, suggestion: Suggestion, cycle_id: str, **kwargs) -> dict:
        ledger.append(suggestion, cycle_id, 2400.0, "chart.png", **kwargs)
        row = ledger.get_suggestion_by_cycle_id(cycle_id)
        assert row is not None
        return row

    def test_reason_code_round_trips(self) -> None:
        row = self._append(
            Suggestion.no_trade("filler", reason_code="not_evaluated"),
            "c1",
            executed=False,
        )
        self.assertEqual(row["reason_code"], "not_evaluated")

    def test_reason_code_is_derived_when_caller_omits_it(self) -> None:
        row = self._append(Suggestion.no_trade("parse_error: bad R/R"), "c2")
        self.assertEqual(row["reason_code"], "validation_rejected")

    def test_explicit_argument_overrides_the_suggestion(self) -> None:
        row = self._append(
            _trade("ETH-USD"), "c3", executed=False, reason_code="watchdog_shadow"
        )
        self.assertEqual(row["reason_code"], "watchdog_shadow")

    def test_abstention_funnel_is_groupable(self) -> None:
        """The point of the column: counting causes without matching prose."""
        self._append(_trade("ETH-USD"), "t1")
        self._append(Suggestion.no_trade("parse_error: x"), "t2")
        self._append(Suggestion.no_trade("parse_error: y"), "t3")
        self._append(
            Suggestion.no_trade("f", reason_code="not_evaluated"), "t4", executed=False
        )

        with ledger._connect() as conn:
            counts = dict(
                conn.execute(
                    "SELECT reason_code, COUNT(*) FROM suggestions GROUP BY reason_code"
                ).fetchall()
            )
        self.assertEqual(
            counts, {"trade": 1, "validation_rejected": 2, "not_evaluated": 1}
        )

    def test_every_emitted_code_is_in_the_documented_vocabulary(self) -> None:
        emitted = {
            ledger.classify_reason(s)
            for s in agent.select_decisions([_trade("ETH-USD")])
        }
        self.assertTrue(emitted <= set(ledger.REASON_CODES), emitted)


if __name__ == "__main__":
    unittest.main()
