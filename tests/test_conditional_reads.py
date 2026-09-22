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


class TestArmedReads(unittest.TestCase):
    """2026-09-22: an untested array arms the read instead of dropping it."""

    def _untested(self, **kw) -> dict:
        return _candidate(state="untested", price=103.0, **kw)

    def test_untested_array_arms_instead_of_dropping(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H1", self._untested(),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertIsNone(read["bias"])
        self.assertEqual(read["armed_bias"], "bullish")
        self.assertIsNone(read["dropped_reason"])
        self.assertEqual(read["invalidation_price"], 99.0)
        self.assertFalse(read["stale_invalidation"])

    def test_holding_array_stays_live_not_armed(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H1", _candidate(),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertEqual(read["bias"], "bullish")
        self.assertIsNone(read["armed_bias"])

    def test_armed_read_still_obeys_the_side_and_draw_rules(self) -> None:
        opposed = conditional.assemble_read(
            "BTC-USD", "H1", self._untested(),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bearish"},
        )
        self.assertIsNone(opposed["armed_bias"])
        self.assertIn("opposes", opposed["dropped_reason"])
        wrong_side = conditional.assemble_read(
            "BTC-USD", "H1", self._untested(draw_at=80.0),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertIsNone(wrong_side["armed_bias"])
        self.assertIn("wrong side", wrong_side["dropped_reason"])

    def test_traded_through_is_still_dropped(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H1", _candidate(state="traded_through"),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertIsNone(read["bias"])
        self.assertIsNone(read["armed_bias"])

    def test_arming_to_live_changes_the_dedup_key(self) -> None:
        """Price reaching the array is a new observation, not a re-print."""
        choice = {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"}
        armed = conditional.assemble_read("BTC-USD", "H1", self._untested(), choice)
        live = conditional.assemble_read("BTC-USD", "H1", _candidate(), choice)
        self.assertNotEqual(armed["dedup_key"], live["dedup_key"])


class TestDrawSideRule(unittest.TestCase):
    """2026-09-21 fix: a bias whose draw sits on the wrong side is refused.

    Fixture prices are the three recorded 09-18 reads that motivated it:
    bullish bias on the 80,471-81,128 array with the draw at 78,124 — a
    bullish read drawing DOWN, which the LLM path accepted and stored.
    """

    def _sept18_candidate(self) -> dict:
        return {
            "price": 80800.0,
            "location": "premium",
            "range_lo": 78000.0,
            "range_hi": 82000.0,
            "repelling": [
                {"kind": "order_block", "side": "bullish",
                 "lo": 80471.0, "hi": 81128.0, "state": "holding"}
            ],
            "attracting": [
                {"kind": "sellside_pool", "lo": 78124.0, "hi": 78200.0,
                 "touches": 2}
            ],
        }

    def test_the_recorded_bad_read_is_now_refused(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H1", self._sept18_candidate(),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertIsNone(read["bias"])
        self.assertIn("wrong side", read["dropped_reason"])

    def test_bearish_draw_above_price_is_refused(self) -> None:
        candidate = self._sept18_candidate()
        candidate["repelling"][0]["side"] = "bearish"
        candidate["attracting"][0] = {
            "kind": "buyside_pool", "lo": 81500.0, "hi": 81600.0, "touches": 2}
        read = conditional.assemble_read(
            "BTC-USD", "H1", candidate,
            {"repelling_id": 0, "attracting_id": 0, "bias": "bearish"},
        )
        self.assertIsNone(read["bias"])
        self.assertIn("wrong side", read["dropped_reason"])

    def test_correctly_oriented_bias_still_passes(self) -> None:
        candidate = self._sept18_candidate()
        candidate["attracting"][0] = {
            "kind": "buyside_pool", "lo": 81500.0, "hi": 81600.0, "touches": 2}
        read = conditional.assemble_read(
            "BTC-USD", "H1", candidate,
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertEqual(read["bias"], "bullish")
        self.assertIsNone(read["dropped_reason"])


class TestDedup(TempDbTestCase):
    def _read(self, bias="bullish", lo=99.0):
        candidate = _candidate()
        candidate["repelling"][0]["lo"] = lo
        return conditional.assemble_read(
            "BTC-USD", "H4", candidate,
            {"repelling_id": 0, "attracting_id": 0, "bias": bias},
        )

    def test_unchanged_setup_is_not_reinserted(self) -> None:
        bars = _bars([100 + (4 if i % 2 else 0) for i in range(120)])
        with mock.patch("anthropic.Anthropic") as cls:
            cls.return_value.messages.create.side_effect = RuntimeError("down")
            conditional.run_conditional_cycle(
                "2026-09-21T15:00:00Z", {"BTC-USD": {"H4": bars, "H1": bars}})
            first = len(store.read_history())
            conditional.run_conditional_cycle(
                "2026-09-21T15:30:00Z", {"BTC-USD": {"H4": bars, "H1": bars}})
            second = len(store.read_history())
        self.assertGreater(first, 0)
        self.assertEqual(first, second)   # identical tape -> no new rows

    def test_key_changes_when_an_anchor_moves(self) -> None:
        a = self._read(lo=99.0)
        b = self._read(lo=98.5)
        self.assertNotEqual(a["dedup_key"], b["dedup_key"])

    def test_key_changes_when_bias_resolves_away(self) -> None:
        """Invalidation flips state -> bias nulls -> key changes -> slot reopens."""
        a = self._read(bias="bullish")
        candidate = _candidate(state="traded_through")
        b = conditional.assemble_read(
            "BTC-USD", "H4", candidate,
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        self.assertIsNone(b["bias"])
        self.assertNotEqual(a["dedup_key"], b["dedup_key"])


class TestWidenedCandidates(unittest.TestCase):
    def test_h1_sees_h4_arrays_tagged(self) -> None:
        """The merge + tag logic, with the detector mocked.

        Synthetic sawtooths do not produce MSB structure, so driving the real
        detector here would test candle geometry rather than the merge. The
        detector has its own suite (test_htf_structure); what must hold HERE
        is that an H4-detected zone reaches the H1 candidate list tagged, and
        an H1 zone does not get the tag.
        """
        class _Zone:
            zone_type, direction = "order_block", "bullish"
            mitigated = False
            def __init__(self, lo, hi):
                self.low, self.hi = lo, hi
                self.high = hi

        calls = []

        def fake_zones(bars, lookback=60, product_id=None, **kw):
            calls.append(len(bars))
            # 60 bars -> the H1 call; 80 -> the H4 call (distinguished below)
            return [_Zone(99.0, 100.0)] if len(bars) == 60 else [_Zone(95.0, 96.0)]

        h1_bars = _bars([100.0] * 60)
        h4_bars = _bars([100.0] * 80)
        with mock.patch.object(conditional, "detect_htf_zones",
                               side_effect=fake_zones):
            out = conditional.build_candidates(
                {"BTC-USD": {"H4": h4_bars, "H1": h1_bars}}
            )
        h1 = out[("BTC-USD", "H1")]
        kinds = [z["kind"] for z in h1["repelling"]]
        self.assertIn("order_block", kinds)          # native H1, untagged
        self.assertIn("order_block@H4", kinds)       # merged, tagged
        h4 = out[("BTC-USD", "H4")]
        self.assertTrue(
            all(not z["kind"].endswith("@H4") for z in h4["repelling"]),
            "the H4 read's own candidates must not carry the merge tag",
        )
        # lookback widened to 120 on every detector call
        with mock.patch.object(conditional, "detect_htf_zones",
                               side_effect=fake_zones) as m:
            conditional.build_candidates({"BTC-USD": {"H4": h4_bars,
                                                      "H1": h1_bars}})
            for call in m.call_args_list:
                self.assertEqual(call.kwargs.get("lookback"), 120)

    def test_holding_by_proximity(self) -> None:
        class _Z:
            zone_type, direction = "order_block", "bullish"
            low, high, mitigated = 99.0, 100.0, False

        # price 0.1 above the zone with a buffer of 0.2 -> holding now
        self.assertEqual(
            conditional._array_state(_Z(), 100.1, 100.1, atr_buffer=0.2),
            "holding")
        # same geometry with no buffer -> the old strict answer
        self.assertEqual(
            conditional._array_state(_Z(), 100.1, 100.1, atr_buffer=0.0),
            "untested")


class TestProgrammaticSelection(unittest.TestCase):
    def test_picks_the_nearest_holding_array(self) -> None:
        choice = conditional._programmatic_choice(_candidate())
        self.assertEqual(choice["repelling_id"], 0)
        self.assertEqual(choice["bias"], "bullish")

    def test_falls_back_to_an_untested_array_which_assembles_armed(self) -> None:
        candidate = _candidate(state="untested", price=103.0)
        choice = conditional._programmatic_choice(candidate)
        self.assertEqual(choice["repelling_id"], 0)
        read = conditional.assemble_read("BTC-USD", "H4", candidate, choice)
        self.assertIsNone(read["bias"])
        self.assertEqual(read["armed_bias"], "bullish")

    def test_withholds_bias_when_every_array_is_traded_through(self) -> None:
        choice = conditional._programmatic_choice(
            _candidate(state="traded_through")
        )
        self.assertIsNone(choice["bias"])
        self.assertIsNone(choice["repelling_id"])

    def test_prefers_holding_over_a_nearer_untested_array(self) -> None:
        candidate = _candidate(state="untested")
        candidate["repelling"].append(
            {"kind": "breaker", "side": "bullish", "lo": 95.0, "hi": 96.0,
             "state": "holding"})
        choice = conditional._programmatic_choice(candidate)
        self.assertEqual(choice["repelling_id"], 1)

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
            "BTC-USD", "H1", _candidate(state="traded_through"),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        store.insert_reads("2026-09-17T12:00:00Z", [read])
        row = store.latest_reads()[0]
        self.assertIsNone(row["bias"])
        self.assertIsNone(row["armed_bias"])
        self.assertIsNotNone(row["dropped_reason"])

    def test_armed_read_persists_with_null_bias(self) -> None:
        read = conditional.assemble_read(
            "BTC-USD", "H1", _candidate(state="untested", price=103.0),
            {"repelling_id": 0, "attracting_id": 0, "bias": "bullish"},
        )
        store.insert_reads("2026-09-17T12:00:00Z", [read])
        row = store.latest_reads()[0]
        self.assertIsNone(row["bias"])
        self.assertEqual(row["armed_bias"], "bullish")
        self.assertEqual(row["invalidation_price"], 99.0)

    def test_old_books_gain_the_armed_column(self) -> None:
        import sqlite3

        with sqlite3.connect(config.LEDGER_DB) as conn:
            conn.execute("DROP TABLE intel_reads")
            conn.execute(
                "CREATE TABLE intel_reads (id INTEGER PRIMARY KEY, cycle_ts TEXT, "
                "product_id TEXT, timeframe TEXT, bias TEXT, created_at TEXT)")
        store.init_db()
        with sqlite3.connect(config.LEDGER_DB) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(intel_reads)")}
        self.assertIn("armed_bias", cols)
        self.assertIn("dedup_key", cols)

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


class TestArmedScorer(unittest.TestCase):
    """Bullish array 99-101, draw 110-111, invalidation 99, published at 103."""

    def _read(self, **kw) -> dict:
        read = {
            "id": 7, "product_id": "BTC-USD", "timeframe": "H1",
            "bias": None, "armed_bias": "bullish", "spot": 103.0,
            "repelling_lo": 99.0, "repelling_hi": 101.0,
            "attracting_lo": 110.0, "attracting_hi": 111.0,
            "invalidation_price": 99.0,
        }
        read.update(kw)
        return read

    def _resolve(self, triples, **kw):
        from intelligence import read_scorer

        return read_scorer.resolve_armed(self._read(), _m5(triples), **kw)

    def test_never_returning_is_not_triggered(self) -> None:
        out = self._resolve([(104, 102, 103), (105, 102, 104)])
        self.assertEqual(out["outcome"], "armed_not_triggered")
        self.assertIsNone(out["trigger_bar"])

    def test_draw_before_retrace_is_ran_to_draw_not_a_hit(self) -> None:
        out = self._resolve([(104, 102, 103), (112, 104, 110), (101, 99.5, 100)])
        self.assertEqual(out["outcome"], "armed_ran_to_draw")
        self.assertIsNone(out["trigger_bar"])

    def test_retrace_then_draw_is_a_hit(self) -> None:
        out = self._resolve([(104, 102, 103), (103, 100.5, 101.5), (112, 104, 110)])
        self.assertEqual(out["outcome"], "resolved_target")
        self.assertEqual(out["trigger_bar"], 1)
        self.assertEqual(out["bars_to_outcome"], 2)

    def test_retrace_then_close_through_is_a_miss(self) -> None:
        out = self._resolve([(104, 102, 103), (103, 100.5, 101.5), (100, 97, 98)])
        self.assertEqual(out["outcome"], "resolved_invalidated")

    def test_trigger_bar_closing_through_is_a_miss(self) -> None:
        out = self._resolve([(104, 102, 103), (103, 97, 98), (112, 104, 110)])
        self.assertEqual(out["outcome"], "resolved_invalidated")
        self.assertEqual(out["bars_to_outcome"], 1)

    def test_arm_window_bounds_the_trigger(self) -> None:
        # 13 quiet bars then a retrace; with a 1h window (12 bars) it is too late.
        quiet = [(104, 102, 103)] * 13
        out = self._resolve(quiet + [(103, 100.5, 101.5)], arm_window_h=1)
        self.assertEqual(out["outcome"], "armed_not_triggered")

    def test_geometry_is_measured_from_the_touched_edge(self) -> None:
        out = self._resolve([(104, 102, 103), (103, 100.5, 101.5), (112, 104, 110)])
        # entry 101: target 110.5 is 9.5 away, invalidation 99 is 2 away
        self.assertAlmostEqual(out["implied_prob"], 2.0 / 11.5, places=6)

    def test_summary_separates_trigger_rate_from_accuracy(self) -> None:
        from intelligence import read_scorer

        rows = [
            {"outcome": "armed_not_triggered", "trigger_bar": None},
            {"outcome": "armed_ran_to_draw", "trigger_bar": None},
            {"outcome": "resolved_target", "trigger_bar": 3, "implied_prob": 0.2},
            {"outcome": "resolved_invalidated", "trigger_bar": 1, "implied_prob": 0.2},
        ]
        for r in rows:
            r["created_at"] = "2026-09-22T12:00:00Z"
        s = read_scorer.summarize_armed(rows)
        self.assertEqual(s["n_armed"], 4)
        self.assertAlmostEqual(s["trigger_rate"], 0.5)
        self.assertEqual(s["n_decided"], 2)
        self.assertAlmostEqual(s["accuracy"], 0.5)
        self.assertAlmostEqual(s["skill_over_geometry"], 0.3)
        self.assertIn("NOT INFERENTIAL", s["inference"])


class TestBarrierImpliedBaseline(unittest.TestCase):
    """Raw 'target before invalidation' is geometry until this is netted off."""

    def _read(self, target: float, inval: float, spot: float = 100.0) -> dict:
        return {"spot": spot, "attracting_lo": target, "attracting_hi": target,
                "invalidation_price": inval}

    def test_symmetric_barriers_are_a_coin_flip(self) -> None:
        from intelligence import read_scorer

        p = read_scorer.barrier_implied_hit_prob(self._read(110.0, 90.0))
        self.assertAlmostEqual(p, 0.5, places=6)

    def test_near_target_far_stop_is_mostly_free(self) -> None:
        """The exact trap: 65% accuracy here would be zero skill."""
        from intelligence import read_scorer

        p = read_scorer.barrier_implied_hit_prob(self._read(101.0, 96.0))
        self.assertAlmostEqual(p, 4.0 / 5.0, places=6)

    def test_far_target_near_stop_is_mostly_hopeless(self) -> None:
        from intelligence import read_scorer

        p = read_scorer.barrier_implied_hit_prob(self._read(110.0, 99.0))
        self.assertAlmostEqual(p, 1.0 / 11.0, places=6)

    def test_missing_anchors_give_no_baseline(self) -> None:
        from intelligence import read_scorer

        self.assertIsNone(read_scorer.barrier_implied_hit_prob(
            {"spot": 100.0, "invalidation_price": None,
             "attracting_lo": None, "attracting_hi": None}))

    def test_skill_is_accuracy_minus_geometry(self) -> None:
        from intelligence import read_scorer

        # Four decided reads, all hits, but geometry alone implied 80%.
        resolved = [{
            "id": i, "product_id": "BTC-USD", "timeframe": "H4",
            "bias": "bullish", "outcome": "resolved_target",
            "mfe_pct": 1.0, "mae_pct": -0.2, "implied_prob": 0.8,
            "created_at": "2026-09-17T12:00:00Z",
        } for i in range(4)]
        summary = read_scorer.summarize(
            resolved, [(None, {"stale_invalidation": 0})] * 4
        )
        self.assertEqual(summary["conditional_accuracy"], 1.0)
        self.assertAlmostEqual(summary["barrier_implied_accuracy"], 0.8)
        self.assertAlmostEqual(summary["skill_over_geometry"], 0.2)

    def test_skill_is_none_without_a_baseline(self) -> None:
        from intelligence import read_scorer

        resolved = [{
            "id": 1, "product_id": "BTC-USD", "timeframe": "H4",
            "bias": "bullish", "outcome": "resolved_target",
            "mfe_pct": 1.0, "mae_pct": -0.2, "implied_prob": None,
            "created_at": "2026-09-17T12:00:00Z",
        }]
        summary = read_scorer.summarize(
            resolved, [(None, {"stale_invalidation": 0})]
        )
        self.assertIsNone(summary["skill_over_geometry"])


class TestCounterfactualStore(TempDbTestCase):
    def test_agreement_is_derived_not_passed(self) -> None:
        store.insert_read_counterfactual(
            "hq", product_id="BTC-USD", timeframe="H4",
            actual="bullish", counterfactual="bullish", ref="c1")
        store.insert_read_counterfactual(
            "hq", product_id="ETH-USD", timeframe="H4",
            actual="bullish", counterfactual="bearish", ref="c2")
        rows = {r["ref"]: r for r in store.read_counterfactuals()}
        self.assertEqual(rows["c1"]["agreed"], 1)
        self.assertEqual(rows["c2"]["agreed"], 0)

    def test_abstention_is_not_a_disagreement(self) -> None:
        """A withheld bias has nothing to disagree with."""
        store.insert_read_counterfactual(
            "hq", product_id="BTC-USD", actual="bullish", counterfactual=None)
        store.insert_read_counterfactual(
            "hq", product_id="BTC-USD", actual=None, counterfactual="bullish")
        for row in store.read_counterfactuals():
            self.assertIsNone(row["agreed"])

    def test_filtered_by_consumer(self) -> None:
        store.insert_read_counterfactual(
            "hq", product_id="BTC-USD", actual="bullish", counterfactual="bullish")
        store.insert_read_counterfactual(
            "mill", product_id="BTC-USD", actual="bearish", counterfactual="bearish")
        self.assertEqual(len(store.read_counterfactuals(consumer="hq")), 1)
        self.assertEqual(len(store.read_counterfactuals()), 2)


if __name__ == "__main__":
    unittest.main()
