"""Phase 2 conditional reads: candidate building, the null rule, persistence.

The load-bearing properties are that the model cannot emit a price no
detector found, and that a bias predicated on a dead array is refused in code
rather than trusted to the prompt.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot_config
import config
from intelligence import conditional, store


def _bars(closes: list[float], *, vol: float = 100.0) -> list[dict]:
    """Hourly bars with a small symmetric wick either side of the close."""
    out = []
    for i, c in enumerate(closes):
        out.append({
            "ts": f"2026-09-{(i // 24) + 1:02d}T{i % 24:02d}:00:00Z",
            "open": c,
            "high": c * 1.002,
            "low": c * 0.998,
            "close": c,
            "volume": vol,
        })
    return out


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


def _candidate(
    *,
    state: str = "holding",
    side: str = "bullish",
    price: float = 100.0,
    draw_at: float = 110.0,
) -> dict:
    return {
        "price": price,
        "location": "discount",
        "range_lo": 90.0,
        "range_hi": 120.0,
        "repelling": [
            {"kind": "order_block", "side": side, "lo": 99.0, "hi": 101.0,
             "state": state}
        ],
        "attracting": [
            {"kind": "buyside_pool", "lo": draw_at, "hi": draw_at + 1,
             "touches": 2}
        ],
    }


class TestNullRule(unittest.TestCase):
    def test_bias_survives_a_holding_array_with_a_draw(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H4", _candidate(),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertEqual(read["bias"], "bullish")
        self.assertEqual(read["invalidation_price"], 99.0)
        self.assertIsNone(read["dropped_reason"])

    def test_bias_refused_when_the_array_is_traded_through(self) -> None:
        """The conditional-logic defect the old schema could not even express."""
        read = conditional.assemble_read(
            "BTC-USD", "H4", _candidate(state="traded_through"),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertIsNone(read["bias"])
        self.assertIn("traded_through", read["dropped_reason"])

    def test_bias_refused_without_a_draw_on_liquidity(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H4", _candidate(),
            {"repelling_id": 0, "attracting_id": None, "bias": "bullish"},
        )
        self.assertIsNone(read["bias"])
        self.assertIn("both anchors", read["dropped_reason"])

    def test_bias_refused_when_the_array_opposes_it(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H4", _candidate(side="bearish"),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertIsNone(read["bias"])
        self.assertIn("opposes", read["dropped_reason"])

    def test_out_of_range_index_cannot_invent_a_level(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H4", _candidate(),
            {"repelling_id": 99, "attracting_id": -5, "bias": "bullish"},
        )
        self.assertIsNone(read["bias"])
        self.assertIsNone(read["repelling_lo"])
        self.assertIsNone(read["attracting_lo"])

    def test_prices_always_come_from_the_candidate_list(self) -> None:
        """A price in the reply is ignored; only indices are honoured."""
        read = conditional.assemble_read(
            "BTC-USD", "H4", _candidate(),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish",
             "invalidation_price": 12345.0, "attracting_lo": 999.0},
        )
        self.assertEqual(read["invalidation_price"], 99.0)
        self.assertEqual(read["attracting_lo"], 110.0)

    def test_stale_invalidation_is_computed_not_asked(self) -> None:
        """Price already below a bullish array's floor = thesis dead on arrival."""
        candidate = _candidate(price=98.0)
        read = conditional.assemble_read(
            "BTC-USD", "H4", candidate,
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        # 98 is outside 99-101, so the array reads untested and bias is refused
        # — which is itself the guard. Force the bias through to check the flag.
        candidate["repelling"][0]["state"] = "holding"
        read = conditional.assemble_read(
            "BTC-USD", "H4", candidate,
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertEqual(read["bias"], "bullish")
        self.assertTrue(read["stale_invalidation"])


class TestProgrammaticSelection(unittest.TestCase):
    def test_picks_the_nearest_holding_array(self) -> None:
        choice = conditional._programmatic_choice(_candidate())
        self.assertEqual(choice["repelling_id"], 0)
        self.assertEqual(choice["bias"], "bullish")

    def test_withholds_bias_with_no_holding_array(self) -> None:
        choice = conditional._programmatic_choice(
            _candidate(state="untested")
        )
        self.assertIsNone(choice["bias"])
        self.assertIsNone(choice["repelling_id"])

    def test_withholds_bias_when_no_draw_sits_on_the_array_side(self) -> None:
        """A bullish array with only downside liquidity is not a long."""
        choice = conditional._programmatic_choice(_candidate(draw_at=80.0))
        self.assertIsNone(choice["bias"])


class TestCandidateBuilding(unittest.TestCase):
    def test_pools_need_two_equal_extremes(self) -> None:
        import research

        # Sawtooth: repeated equal highs and lows, so pools should form.
        closes = [100 + (4 if i % 2 else 0) for i in range(80)]
        df = research.to_dataframe(_bars(closes))
        pools = conditional._liquidity_pools(df, price=100.0)
        self.assertTrue(all(p["touches"] >= 2 for p in pools))
        self.assertTrue(all(p["kind"].endswith("_pool") for p in pools))

    def test_pools_are_only_unswept_ones(self) -> None:
        import research

        closes = [100 + (4 if i % 2 else 0) for i in range(80)]
        df = research.to_dataframe(_bars(closes))
        pools = conditional._liquidity_pools(df, price=100.0)
        for p in pools:
            if p["kind"] == "buyside_pool":
                self.assertGreater(p["hi"], 100.0)
            else:
                self.assertLess(p["lo"], 100.0)

    def test_location_is_premium_at_range_highs(self) -> None:
        import research

        df = research.to_dataframe(_bars([100 + i for i in range(60)]))
        location, lo, hi = conditional._location(df, float(df["close"].iloc[-1]))
        self.assertEqual(location, "premium")
        self.assertLess(lo, hi)

    def test_short_series_yields_no_candidates(self) -> None:
        out = conditional.build_candidates(
            {"BTC-USD": {"H4": _bars([100.0] * 10), "H1": []}}
        )
        self.assertEqual(out, {})

    def test_only_configured_timeframes_are_built(self) -> None:
        bars = _bars([100 + i * 0.5 for i in range(120)])
        with mock.patch.object(
            bot_config, "INTEL_CONDITIONAL_TIMEFRAMES", ("H4",)
        ):
            out = conditional.build_candidates(
                {"BTC-USD": {"H4": bars, "H1": bars, "M15": bars}}
            )
        self.assertEqual(set(out), {("BTC-USD", "H4")})


class TestPersistence(TempDbTestCase):
    def test_reads_roundtrip(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H4", _candidate(),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish",
             "rationale": "OB holding, draw above"},
        )
        store.insert_reads("2026-09-17T12:00:00Z", [read], source="llm")
        rows = store.latest_reads()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bias"], "bullish")
        self.assertEqual(rows[0]["invalidation_trigger"], "m5_close_through")
        self.assertEqual(rows[0]["stale_invalidation"], 0)
        self.assertEqual(rows[0]["source"], "llm")

    def test_null_bias_persists_as_null(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H1", _candidate(state="untested"),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        store.insert_reads("2026-09-17T12:00:00Z", [read])
        row = store.latest_reads()[0]
        self.assertIsNone(row["bias"])
        self.assertIsNotNone(row["dropped_reason"])

    def test_cycle_falls_back_to_programmatic_when_the_llm_fails(self) -> None:
        bars = _bars([100 + (4 if i % 2 else 0) for i in range(120)])
        with mock.patch("anthropic.Anthropic") as cls:
            cls.return_value.messages.create.side_effect = RuntimeError("down")
            result = conditional.run_conditional_cycle(
                "2026-09-17T15:00:00Z",
                {"BTC-USD": {"H4": bars, "H1": bars}},
            )
        self.assertEqual(result["source"], "programmatic")
        self.assertTrue(store.latest_reads())

    def test_cycle_with_no_candidates_writes_nothing(self) -> None:
        result = conditional.run_conditional_cycle(
            "2026-09-17T16:00:00Z", {"BTC-USD": {"H4": [], "H1": []}}
        )
        self.assertEqual(result["source"], "none")
        self.assertEqual(store.latest_reads(), [])


def _m5(prices: list[tuple[float, float, float]]) -> list[dict]:
    """(high, low, close) triples as M5 bars from 2026-09-17T12:05Z."""
    return [
        {"ts": f"2026-09-17T12:{5 + i * 5:02d}:00Z",
         "open": c, "high": h, "low": l, "close": c, "volume": 1.0}
        for i, (h, l, c) in enumerate(prices)
    ]


class TestReadScorer(unittest.TestCase):
    def _read(self, **kw) -> dict:
        read = {
            "id": 1, "product_id": "BTC-USD", "timeframe": "H4",
            "bias": "bullish", "spot": 100.0,
            "attracting_lo": 110.0, "attracting_hi": 111.0,
            "invalidation_price": 95.0,
            "created_at": "2026-09-17T12:00:00Z",
            "stale_invalidation": 0,
        }
        read.update(kw)
        return read

    def test_target_printed_first_is_a_hit(self) -> None:
        from intelligence import read_scorer

        bars = _m5([(101, 99, 100), (112, 108, 110)])
        out = read_scorer.resolve_read(self._read(), bars)
        self.assertEqual(out["outcome"], "resolved_target")
        self.assertEqual(out["bars_to_outcome"], 1)

    def test_invalidation_printed_first_is_a_miss(self) -> None:
        from intelligence import read_scorer

        bars = _m5([(101, 99, 100), (99, 93, 94), (112, 108, 110)])
        out = read_scorer.resolve_read(self._read(), bars)
        self.assertEqual(out["outcome"], "resolved_invalidated")

    def test_same_bar_tie_resolves_against_the_thesis(self) -> None:
        """Conservative tie rule, matching the pack's barrier convention."""
        from intelligence import read_scorer

        bars = _m5([(112, 93, 94)])   # touches target high AND closes through
        out = read_scorer.resolve_read(self._read(), bars)
        self.assertEqual(out["outcome"], "resolved_invalidated")

    def test_wick_through_without_a_close_is_not_invalidation(self) -> None:
        """Our stated convention is close-through; the wick is recorded only."""
        from intelligence import read_scorer

        bars = _m5([(101, 94, 100), (112, 108, 110)])
        out = read_scorer.resolve_read(self._read(), bars)
        self.assertEqual(out["outcome"], "resolved_target")
        self.assertEqual(out["wick_hit"], 0)

    def test_neither_barrier_is_unresolved(self) -> None:
        from intelligence import read_scorer

        out = read_scorer.resolve_read(
            self._read(), _m5([(101, 99, 100), (102, 98, 100)])
        )
        self.assertEqual(out["outcome"], "unresolved")

    def test_bearish_read_resolves_the_other_way(self) -> None:
        from intelligence import read_scorer

        read = self._read(bias="bearish", attracting_lo=90.0,
                          attracting_hi=91.0, invalidation_price=105.0)
        out = read_scorer.resolve_read(read, _m5([(101, 89, 90)]))
        self.assertEqual(out["outcome"], "resolved_target")

    def test_null_bias_read_is_never_directionally_resolved(self) -> None:
        from intelligence import read_scorer

        out = read_scorer.resolve_read(
            self._read(bias=None, invalidation_price=None),
            _m5([(112, 93, 94)]),
        )
        self.assertEqual(out["outcome"], "resolved_target")  # band still touched
        self.assertIsNone(out["bias"])

    def test_summary_refuses_to_look_inferential_on_a_small_sample(self) -> None:
        from intelligence import read_scorer

        resolved = [{
            "id": i, "product_id": "BTC-USD", "timeframe": "H4",
            "bias": "bullish", "outcome": "resolved_target",
            "bars_to_outcome": 1, "wick_hit": None,
            "mfe_pct": 1.0, "mae_pct": -0.5,
            "created_at": "2026-09-17T12:00:00Z",
        } for i in range(5)]
        summary = read_scorer.summarize(
            resolved, [(None, {"stale_invalidation": 0})] * 5
        )
        self.assertEqual(summary["conditional_accuracy"], 1.0)
        self.assertIn("NOT INFERENTIAL", summary["inference"])
        self.assertEqual(summary["n_days"], 1)

    def test_summary_tracks_null_calibration(self) -> None:
        from intelligence import read_scorer

        resolved = [
            {"id": 1, "product_id": "BTC-USD", "timeframe": "H4",
             "bias": "bullish", "outcome": "resolved_target",
             "mfe_pct": 2.0, "mae_pct": -1.0,
             "created_at": "2026-09-17T12:00:00Z"},
            {"id": 2, "product_id": "BTC-USD", "timeframe": "H1",
             "bias": None, "outcome": "unresolved",
             "mfe_pct": 0.2, "mae_pct": -0.1,
             "created_at": "2026-09-17T12:00:00Z"},
        ]
        summary = read_scorer.summarize(
            resolved, [(None, {"stale_invalidation": 0})] * 2
        )
        self.assertEqual(summary["null_bias_rate"], 0.5)
        self.assertGreater(
            summary["range_when_bias_pct"], summary["range_when_null_pct"]
        )


if __name__ == "__main__":
    unittest.main()
