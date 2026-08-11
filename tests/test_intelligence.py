"""Tests for the intelligence layer: stances, funding regimes, store, scheduling."""

from __future__ import annotations

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
    _extract_json,
    _fallback_stances,
    compute_timeframe_features,
    run_stance_cycle,
)
from main import seconds_until_next_hour


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


class TestWallClockScheduling(unittest.TestCase):
    def test_seconds_until_next_hour(self) -> None:
        # 15:30:00 UTC -> 1800s to 16:00.
        ts = 1_754_838_600  # arbitrary epoch at :30 boundary check below
        remainder = ts % 3600
        expected = 3600 - remainder
        self.assertAlmostEqual(seconds_until_next_hour(ts), max(expected, 10.0))

    def test_minimum_guard(self) -> None:
        # 1 second before the hour -> guard kicks in.
        ts = 3600 * 1000 - 1
        self.assertEqual(seconds_until_next_hour(ts), 10.0)


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
