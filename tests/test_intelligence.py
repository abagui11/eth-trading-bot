"""Tests for the intelligence layer: stances, funding regimes, store, scheduling."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot_config
import config
from intelligence import store
from intelligence.funding import (
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_CHOP,
    REGIME_SWITCH_BEAR,
    REGIME_SWITCH_BULL,
    classify_regime,
    evaluate_product,
)
from intelligence.stance import (
    STANCE_PRODUCTS,
    STANCE_TIMEFRAMES,
    _cites_price,
    _extract_json,
    _fallback_stances,
    apply_override_policy,
    compute_timeframe_features,
    run_stance_cycle,
)
import main
from main import seconds_until_next_slot


def _bars(closes: list[float], volume: float = 100.0) -> list[dict]:
    return [
        {
            "ts": f"2026-01-01T{i % 24:02d}:00:00Z",
            "open": c,
            "high": c * 1.01,
            "low": c * 0.99,
            "close": c,
            "volume": volume,
        }
        for i, c in enumerate(closes)
    ]


class TestExtractJson(unittest.TestCase):
    """The stance reply is not always a bare JSON object."""

    def test_plain_object(self) -> None:
        self.assertEqual(_extract_json('{"stances": []}'), {"stances": []})

    def test_code_fenced(self) -> None:
        self.assertEqual(
            _extract_json('```json\n{"stances": []}\n```'), {"stances": []}
        )

    def test_trailing_commentary(self) -> None:
        """Regression: a sentence after the object used to raise 'Extra data'."""
        reply = '{"stances": [], "medium_summary": "x"}\n\nLet me know if you want more.'
        self.assertEqual(
            _extract_json(reply), {"stances": [], "medium_summary": "x"}
        )

    def test_leading_commentary(self) -> None:
        reply = 'Here is the analysis:\n{"stances": []}'
        self.assertEqual(_extract_json(reply), {"stances": []})

    def test_fenced_with_trailing_commentary(self) -> None:
        reply = 'Sure:\n```json\n{"stances": []}\n```\nHope that helps.'
        self.assertEqual(_extract_json(reply), {"stances": []})

    def test_no_object_raises(self) -> None:
        with self.assertRaises(ValueError):
            _extract_json("no json here")

    def test_non_object_raises(self) -> None:
        with self.assertRaises(ValueError):
            _extract_json("[1, 2, 3]")


class TempDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = config.LEDGER_DB
        config.LEDGER_DB = Path(self._tmp.name) / "test_ledger.db"
        store.init_db()

    def tearDown(self) -> None:
        config.LEDGER_DB = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


class TestStanceFeatures(unittest.TestCase):
    def test_uptrend_is_bullish(self) -> None:
        closes = [100 + i * 2.0 for i in range(60)]
        features = compute_timeframe_features(_bars(closes))
        self.assertEqual(features["stance"], "bullish")

    def test_downtrend_is_bearish(self) -> None:
        closes = [220 - i * 2.0 for i in range(60)]
        features = compute_timeframe_features(_bars(closes))
        self.assertEqual(features["stance"], "bearish")

    def test_flat_is_neutral(self) -> None:
        closes = [100.0 + (0.4 if i % 2 else -0.4) for i in range(60)]
        features = compute_timeframe_features(_bars(closes))
        self.assertEqual(features["stance"], "neutral")


class TestStanceCycle(TempDbTestCase):
    def _fake_features(self) -> dict:
        f = compute_timeframe_features(_bars([100 + i for i in range(60)]))
        return {
            p: {tf: dict(f) for tf in STANCE_TIMEFRAMES} for p in STANCE_PRODUCTS
        }

    def test_fallback_covers_all_products_and_timeframes(self) -> None:
        stances = _fallback_stances(self._fake_features())
        keys = {(s["product_id"], s["timeframe"]) for s in stances}
        self.assertEqual(len(keys), len(STANCE_PRODUCTS) * len(STANCE_TIMEFRAMES))

    def test_run_stance_cycle_llm_failure_uses_fallback(self) -> None:
        with mock.patch(
            "intelligence.stance.gather_bars", return_value={}
        ), mock.patch(
            "intelligence.stance.gather_features", return_value=self._fake_features()
        ), mock.patch(
            "intelligence.stance.render_structure_board"
        ), mock.patch("anthropic.Anthropic") as anthropic_cls:
            anthropic_cls.return_value.messages.create.side_effect = RuntimeError(
                "api down"
            )
            result = run_stance_cycle("2026-08-10T15:00:00Z")

        self.assertEqual(result["source"], "programmatic")
        stored = store.latest_stances()
        self.assertEqual(len(stored), 6)
        self.assertTrue(all(s["source"] == "programmatic" for s in stored))
        medium = store.latest_medium_summary()
        self.assertIsNotNone(medium)
        self.assertIn("BTC H4", medium["summary"])

    def test_run_stance_cycle_llm_success(self) -> None:
        payload = {
            "stances": [
                {
                    "product_id": p,
                    "timeframe": tf,
                    "stance": "bullish",
                    "confidence": 0.8,
                    "rationale": "test",
                }
                for p in STANCE_PRODUCTS
                for tf in STANCE_TIMEFRAMES
            ],
            "medium_summary": "BTC leads higher.",
            "btc_eth_note": "ETH follows with beta.",
        }
        import json as _json

        block = mock.Mock()
        block.type = "text"
        block.text = _json.dumps(payload)
        response = mock.Mock()
        response.content = [block]

        with mock.patch(
            "intelligence.stance.gather_bars", return_value={}
        ), mock.patch(
            "intelligence.stance.gather_features", return_value=self._fake_features()
        ), mock.patch(
            "intelligence.stance.render_structure_board"
        ), mock.patch("anthropic.Anthropic") as anthropic_cls:
            anthropic_cls.return_value.messages.create.return_value = response
            result = run_stance_cycle("2026-08-10T16:00:00Z")

        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["medium_summary"], "BTC leads higher.")
        stored = store.latest_stances()
        self.assertEqual(len(stored), 6)
        self.assertTrue(all(s["stance"] == "bullish" for s in stored))

    def _run_with_llm_stance(self, stance: str, **extra) -> list[dict]:
        """Drive the real cycle with the model returning `stance` everywhere."""
        import json as _json

        payload = {
            "stances": [
                {"product_id": p, "timeframe": tf, "stance": stance,
                 "confidence": 0.7, "rationale": "test", **extra}
                for p in STANCE_PRODUCTS
                for tf in STANCE_TIMEFRAMES
            ],
            "medium_summary": "s",
            "btc_eth_note": "n",
        }
        block = mock.Mock()
        block.type = "text"
        block.text = _json.dumps(payload)
        response = mock.Mock()
        response.content = [block]
        with mock.patch(
            "intelligence.stance.gather_bars", return_value={}
        ), mock.patch(
            "intelligence.stance.gather_features", return_value=self._fake_features()
        ), mock.patch(
            "intelligence.stance.render_structure_board"
        ), mock.patch("anthropic.Anthropic") as anthropic_cls:
            anthropic_cls.return_value.messages.create.return_value = response
            run_stance_cycle("2026-09-17T12:00:00Z")
        return store.latest_stances()

    def test_counterfactual_is_recorded_end_to_end(self) -> None:
        """Flags off through the real cycle: override published AND logged."""
        # _fake_features is a clean uptrend, so the deterministic score is bullish.
        with mock.patch.object(bot_config, "STANCE_PUBLISH_DETERMINISTIC", False):
            stored = self._run_with_llm_stance("neutral")
        self.assertTrue(all(s["stance"] == "neutral" for s in stored))
        self.assertTrue(all(s["det_stance"] == "bullish" for s in stored))
        self.assertTrue(all(s["llm_stance"] == "neutral" for s in stored))
        self.assertTrue(
            all(s["override_kind"] == store.OVERRIDE_MUTED for s in stored)
        )

    def test_publish_deterministic_reverts_through_the_real_cycle(self) -> None:
        """Phase 1 live: consumers get the deterministic score, ledger keeps all."""
        with mock.patch.object(bot_config, "STANCE_PUBLISH_DETERMINISTIC", True):
            stored = self._run_with_llm_stance("bearish")
        self.assertTrue(all(s["stance"] == "bullish" for s in stored))
        # The attempt survives the revert structurally, not just in prose.
        self.assertTrue(all(s["llm_stance"] == "bearish" for s in stored))
        self.assertTrue(
            all(s["override_kind"] == store.OVERRIDE_FLIPPED for s in stored)
        )
        self.assertTrue(
            all("[reverted:bearish]" in s["override_reason"] for s in stored)
        )


class TestOverridePolicy(unittest.TestCase):
    """INTEL_BOARD_PLAN Phase 0/1: counterfactual logging and override gating."""

    def _features(self, det: str, score: int = 2) -> dict:
        cell = {"stance": det, "score": score, "range_pos": 0.5,
                "higher_highs": True, "lower_lows": False}
        return {"BTC-USD": {"H1": cell}}

    def test_confidence_moves_with_a_reverted_stance(self) -> None:
        """A reverted row must not carry the model's confidence in its old call.

        Downstream gates (Kalshi eva_wick thresholds on m15 confidence) read
        that number as conviction in the stance beside it.
        """
        with mock.patch.object(bot_config, "STANCE_PUBLISH_DETERMINISTIC", True):
            out = apply_override_policy(
                self._row("neutral", confidence=0.7),
                self._features("bullish", score=3),
            )
        self.assertEqual(out[0]["stance"], "bullish")
        self.assertEqual(out[0]["confidence"], 1.0)   # |3|/3, not the LLM's 0.7

    def test_confidence_untouched_when_the_override_stands(self) -> None:
        with mock.patch.object(bot_config, "STANCE_PUBLISH_DETERMINISTIC", False), \
                mock.patch.object(
                    bot_config, "STANCE_OVERRIDE_REQUIRE_EVIDENCE", False):
            out = apply_override_policy(
                self._row("neutral", confidence=0.7), self._features("bullish")
            )
        self.assertEqual(out[0]["confidence"], 0.7)

    def _row(self, stance: str, **kw) -> list[dict]:
        row = {"product_id": "BTC-USD", "timeframe": "H1", "stance": stance}
        row.update(kw)
        return [row]

    def test_cites_price_needs_a_real_level(self) -> None:
        self.assertTrue(_cites_price("H4 OB 63,433-64,188 holding"))
        self.assertTrue(_cites_price("reclaimed 2401.5"))
        self.assertFalse(_cites_price("order block is holding"))
        self.assertFalse(_cites_price("at the 0.618 fib"))
        self.assertFalse(_cites_price(None))

    def test_counterfactual_attached_without_changing_stance(self) -> None:
        """Both flags off must annotate only, never rewrite."""
        with mock.patch.object(bot_config, "STANCE_PUBLISH_DETERMINISTIC", False), \
                mock.patch.object(
                    bot_config, "STANCE_OVERRIDE_REQUIRE_EVIDENCE", False):
            out = apply_override_policy(
                self._row("bearish"), self._features("bullish")
            )
        self.assertEqual(out[0]["stance"], "bearish")
        self.assertEqual(out[0]["det_stance"], "bullish")

    def test_unevidenced_override_reverts_when_evidence_required(self) -> None:
        with mock.patch.object(bot_config, "STANCE_PUBLISH_DETERMINISTIC", False), \
                mock.patch.object(
                    bot_config, "STANCE_OVERRIDE_REQUIRE_EVIDENCE", True):
            out = apply_override_policy(
                self._row("neutral", override_reason="looks toppy"),
                self._features("bullish"),
            )
        self.assertEqual(out[0]["stance"], "bullish")
        # The attempt must survive the revert, or the ledger loses it.
        self.assertIn("[reverted:neutral]", out[0]["override_reason"])

    def test_evidenced_override_stands_when_only_evidence_required(self) -> None:
        with mock.patch.object(bot_config, "STANCE_PUBLISH_DETERMINISTIC", False), \
                mock.patch.object(
                    bot_config, "STANCE_OVERRIDE_REQUIRE_EVIDENCE", True):
            out = apply_override_policy(
                self._row("neutral", override_reason="H4 OB 63,433-64,188 holding"),
                self._features("bullish"),
            )
        self.assertEqual(out[0]["stance"], "neutral")

    def test_publish_deterministic_reverts_even_evidenced_overrides(self) -> None:
        with mock.patch.object(bot_config, "STANCE_PUBLISH_DETERMINISTIC", True), \
                mock.patch.object(
                    bot_config, "STANCE_OVERRIDE_REQUIRE_EVIDENCE", True):
            out = apply_override_policy(
                self._row("neutral", override_reason="H4 OB 63,433-64,188 holding"),
                self._features("bullish"),
            )
        self.assertEqual(out[0]["stance"], "bullish")

    def test_agreement_is_never_an_override(self) -> None:
        with mock.patch.object(bot_config, "STANCE_PUBLISH_DETERMINISTIC", True):
            out = apply_override_policy(
                self._row("bullish"), self._features("bullish")
            )
        self.assertEqual(out[0]["stance"], "bullish")
        self.assertIsNone(out[0].get("override_reason"))

    def test_missing_features_leave_the_row_alone(self) -> None:
        """A feature gap must not silently rewrite a published stance."""
        with mock.patch.object(bot_config, "STANCE_PUBLISH_DETERMINISTIC", True):
            out = apply_override_policy(self._row("bearish"), {})
        self.assertEqual(out[0]["stance"], "bearish")
        self.assertIsNone(out[0].get("det_stance"))

    def test_fallback_rows_are_their_own_counterfactual(self) -> None:
        f = compute_timeframe_features(_bars([100 + i for i in range(60)]))
        features = {p: {tf: dict(f) for tf in STANCE_TIMEFRAMES}
                    for p in STANCE_PRODUCTS}
        for row in _fallback_stances(features):
            self.assertEqual(row["det_stance"], row["stance"])


class TestOverridePersistence(TempDbTestCase):
    def test_override_kind_is_derived_from_the_pair(self) -> None:
        store.insert_stances(
            "2026-09-17T12:00:00Z",
            [
                {"product_id": "BTC-USD", "timeframe": "H4",
                 "stance": "neutral", "det_stance": "bullish"},
                {"product_id": "BTC-USD", "timeframe": "H1",
                 "stance": "bearish", "det_stance": "neutral"},
                {"product_id": "BTC-USD", "timeframe": "M15",
                 "stance": "bearish", "det_stance": "bullish"},
                {"product_id": "ETH-USD", "timeframe": "H4",
                 "stance": "bullish", "det_stance": "bullish"},
            ],
        )
        kinds = {
            (r["product_id"], r["timeframe"]): r["override_kind"]
            for r in store.latest_stances()
        }
        self.assertEqual(kinds[("BTC-USD", "H4")], store.OVERRIDE_MUTED)
        self.assertEqual(kinds[("BTC-USD", "H1")], store.OVERRIDE_INVENTED)
        self.assertEqual(kinds[("BTC-USD", "M15")], store.OVERRIDE_FLIPPED)
        self.assertIsNone(kinds[("ETH-USD", "H4")])

    def test_reverted_row_still_records_the_attempt(self) -> None:
        """The load-bearing one: Phase 1 must not blind the Phase 0 ledger."""
        store.insert_stances(
            "2026-09-17T12:00:00Z",
            [{"product_id": "BTC-USD", "timeframe": "H1", "stance": "bullish",
              "det_stance": "bullish", "llm_stance": "neutral",
              "override_reason": "[reverted:neutral] policy"}],
        )
        row = store.latest_stances()[0]
        self.assertEqual(row["stance"], "bullish")       # what consumers see
        self.assertEqual(row["llm_stance"], "neutral")   # what the model wanted
        self.assertEqual(row["override_kind"], store.OVERRIDE_MUTED)

    def test_llm_stance_defaults_to_the_published_stance(self) -> None:
        """Nothing reverted means the model's stance *is* the published one."""
        store.insert_stances(
            "2026-09-17T13:00:00Z",
            [{"product_id": "BTC-USD", "timeframe": "H4", "stance": "bearish",
              "det_stance": "bearish"}],
        )
        row = store.latest_stances()[0]
        self.assertEqual(row["llm_stance"], "bearish")
        self.assertIsNone(row["override_kind"])

    def test_rows_without_a_counterfactual_persist_cleanly(self) -> None:
        store.insert_stances(
            "2026-09-17T13:00:00Z",
            [{"product_id": "BTC-USD", "timeframe": "H4", "stance": "bullish"}],
        )
        row = store.latest_stances()[0]
        self.assertIsNone(row["det_stance"])
        self.assertIsNone(row["override_kind"])

    def test_legacy_book_gains_the_columns(self) -> None:
        """Books written before Phase 0 must migrate, not crash."""
        import sqlite3

        with sqlite3.connect(config.LEDGER_DB) as conn:
            conn.execute("DROP TABLE intel_stances")
            conn.execute(
                "CREATE TABLE intel_stances ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_ts TEXT NOT NULL,"
                "product_id TEXT NOT NULL, timeframe TEXT NOT NULL,"
                "stance TEXT NOT NULL, confidence REAL, rationale TEXT,"
                "source TEXT NOT NULL DEFAULT 'llm', created_at TEXT NOT NULL)"
            )
            conn.commit()
        store.insert_stances(
            "2026-09-17T14:00:00Z",
            [{"product_id": "BTC-USD", "timeframe": "H4", "stance": "neutral",
              "det_stance": "bearish"}],
        )
        row = store.latest_stances()[0]
        self.assertEqual(row["det_stance"], "bearish")
        self.assertEqual(row["llm_stance"], "neutral")
        self.assertEqual(row["override_kind"], store.OVERRIDE_MUTED)


class TestFundingRegimes(TempDbTestCase):
    def test_persistent_positive_is_bull(self) -> None:
        rates = [0.01] * 12
        regime, streak = classify_regime(rates)
        self.assertEqual(regime, REGIME_BULL)
        self.assertEqual(streak, 12)

    def test_persistent_negative_is_bear(self) -> None:
        rates = [-0.01] * 10
        regime, _ = classify_regime(rates)
        self.assertEqual(regime, REGIME_BEAR)

    def test_constant_flipping_is_chop(self) -> None:
        rates = [0.01 if i % 2 else -0.01 for i in range(20)]
        regime, _ = classify_regime(rates)
        self.assertEqual(regime, REGIME_CHOP)

    def test_first_confirmed_switch_after_persistence(self) -> None:
        persist = bot_config.FUNDING_PERSIST_PERIODS
        confirm = bot_config.FUNDING_SWITCH_CONFIRM_PERIODS
        rates = [0.01] * persist + [-0.01] * confirm
        regime, streak = classify_regime(rates)
        self.assertEqual(regime, REGIME_SWITCH_BEAR)
        self.assertEqual(streak, confirm)

        rates = [-0.01] * persist + [0.01] * confirm
        regime, _ = classify_regime(rates)
        self.assertEqual(regime, REGIME_SWITCH_BULL)

    def test_unconfirmed_switch_is_still_chop(self) -> None:
        persist = bot_config.FUNDING_PERSIST_PERIODS
        rates = [0.01] * persist + [-0.01]  # only 1 print of the new sign
        regime, _ = classify_regime(rates)
        self.assertEqual(regime, REGIME_CHOP)

    def test_switch_event_fires_once(self) -> None:
        persist = bot_config.FUNDING_PERSIST_PERIODS
        confirm = bot_config.FUNDING_SWITCH_CONFIRM_PERIODS
        series = [
            {"ts": f"2026-08-{i + 1:02d}T00:00:00Z", "rate": 0.01}
            for i in range(persist)
        ] + [
            {"ts": f"2026-08-{persist + i + 1:02d}T00:00:00Z", "rate": -0.01}
            for i in range(confirm)
        ]
        first = evaluate_product("BTC-USD", series)
        self.assertEqual(first.regime, REGIME_SWITCH_BEAR)
        self.assertTrue(first.is_switch_event)

        # Same state re-scanned: no repeat event.
        second = evaluate_product("BTC-USD", series)
        self.assertEqual(second.regime, REGIME_SWITCH_BEAR)
        self.assertFalse(second.is_switch_event)


class TestStore(TempDbTestCase):
    def test_stances_roundtrip(self) -> None:
        store.insert_stances(
            "2026-08-10T15:00:00Z",
            [
                {
                    "product_id": "BTC-USD",
                    "timeframe": "H4",
                    "stance": "bullish",
                    "confidence": 0.9,
                    "rationale": "up only",
                }
            ],
        )
        latest = store.latest_stances()
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["stance"], "bullish")

    def test_rerun_in_same_hour_collapses_to_newest_row(self) -> None:
        """A restart re-runs the cycle under the same hour-bucketed cycle_ts."""
        cycle_ts = "2026-08-10T15:00:00Z"
        base = [
            {"product_id": p, "timeframe": tf, "stance": "bullish", "confidence": 0.6}
            for p in ("BTC-USD", "ETH-USD")
            for tf in ("H4", "H1", "M15")
        ]
        store.insert_stances(cycle_ts, base, source="llm")
        store.insert_stances(
            cycle_ts,
            [{**s, "stance": "bearish", "confidence": 0.67} for s in base],
            source="programmatic",
        )

        latest = store.latest_stances()
        self.assertEqual(len(latest), 6)
        keys = [(s["product_id"], s["timeframe"]) for s in latest]
        self.assertEqual(len(keys), len(set(keys)))
        # Newest batch wins, so the whole board reflects one coherent run.
        self.assertTrue(all(s["stance"] == "bearish" for s in latest))
        self.assertTrue(all(s["source"] == "programmatic" for s in latest))

    def test_invalid_stance_normalized_to_neutral(self) -> None:
        store.insert_stances(
            "2026-08-10T15:00:00Z",
            [
                {
                    "product_id": "BTC-USD",
                    "timeframe": "H4",
                    "stance": "moon",
                }
            ],
        )
        self.assertEqual(store.latest_stances()[0]["stance"], "neutral")

    def test_zmove_events_roundtrip(self) -> None:
        store.insert_zmove_event(
            "ETH-USD", "volume", 3.1, "2026-08-10T14:00:00Z", detail={"mult": 4.2}
        )
        events = store.recent_zmove_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"]["mult"], 4.2)

    def test_funding_series_dedup(self) -> None:
        rates = [{"ts": "2026-08-10T00:00:00Z", "rate": 0.01}]
        self.assertEqual(store.upsert_funding_rates("BTC-USD", rates), 1)
        self.assertEqual(store.upsert_funding_rates("BTC-USD", rates), 0)

    def test_long_thesis_roundtrip(self) -> None:
        store.insert_long_thesis(
            "2026-08-10", "bull_expansion", {"bias": "bullish"}, chart_path=None
        )
        thesis = store.latest_long_thesis()
        self.assertEqual(thesis["cycle_phase"], "bull_expansion")
        self.assertEqual(thesis["thesis"]["bias"], "bullish")


class TestHourlyJobOrdering(unittest.TestCase):
    """The trade cycle must run before the intelligence work, not after."""

    def _run_hourly(self, *, cycle_side_effect=None) -> list[str]:
        calls: list[str] = []

        def fake_run_cycle():
            calls.append("run_cycle")
            if cycle_side_effect is not None:
                raise cycle_side_effect

        def fake_stance_cycle():
            calls.append("stance")
            return {}

        def fake_bias_refine():
            calls.append("bias")
            return 0

        with mock.patch.object(bot_config, "INTELLIGENCE_ENABLED", True), \
                mock.patch.object(bot_config, "MACRO_CONTEXT_ENABLED", True), \
                mock.patch.object(main, "run_cycle", fake_run_cycle), \
                mock.patch(
                    "intelligence.stance.run_stance_cycle", fake_stance_cycle
                ), \
                mock.patch(
                    "macro.bias_score.run_hourly_bias_refine", fake_bias_refine
                ):
            asyncio.run(main.hourly_job(None))
        return calls

    def test_trade_cycle_runs_first(self) -> None:
        self.assertEqual(self._run_hourly(), ["run_cycle", "stance", "bias"])

    def test_cycle_failure_still_runs_intelligence(self) -> None:
        calls = self._run_hourly(cycle_side_effect=RuntimeError("boom"))
        self.assertEqual(calls, ["run_cycle", "stance", "bias"])


class TestWallClockScheduling(unittest.TestCase):
    def test_seconds_until_next_hour(self) -> None:
        ts = 1_754_838_600  # arbitrary epoch at :30 boundary check below
        remainder = ts % 3600
        expected = 3600 - remainder
        self.assertAlmostEqual(seconds_until_next_slot(3600, ts), max(expected, 10.0))

    def test_minimum_guard(self) -> None:
        # 1 second before the hour -> guard kicks in.
        ts = 3600 * 1000 - 1
        self.assertEqual(seconds_until_next_slot(3600, ts), 10.0)

    def test_half_hour_cadence_fires_on_the_half(self) -> None:
        # A 30-minute cadence has to land on :00 and :30, not drift from start.
        on_the_hour = 3600 * 1000
        self.assertEqual(seconds_until_next_slot(1800, on_the_hour + 60), 1740.0)
        self.assertEqual(seconds_until_next_slot(1800, on_the_hour + 1860), 1740.0)

    def test_interval_floor_rejects_a_runaway_cadence(self) -> None:
        # A misconfigured 0 would otherwise divide by zero and hammer the LLM.
        self.assertEqual(seconds_until_next_slot(0, 3600 * 1000 + 30), 30.0)


class TestInternalGate(unittest.TestCase):
    def test_internal_ids_prefer_internal_env(self) -> None:
        import access

        with mock.patch.object(config, "INTERNAL_TELEGRAM_IDS", [111, 222]):
            self.assertEqual(sorted(access.internal_recipient_ids()), [111, 222])

    def test_internal_ids_fallback_to_allowlist(self) -> None:
        import access

        with mock.patch.object(config, "INTERNAL_TELEGRAM_IDS", []), mock.patch.object(
            config, "ALLOWED_TELEGRAM_IDS", [333]
        ):
            self.assertEqual(access.internal_recipient_ids(), [333])


if __name__ == "__main__":
    unittest.main()
