"""Resolution-engine tests for the Eva variant paper books.

The engine decides every number the experiment reports, so the cases that
matter are the ones that bias a result: stop-vs-target ties, the trailing stop
after a partial fill, and the time exit. A ladder that resolved ties
target-first would manufacture the exact winners this experiment exists to
count.
"""

from __future__ import annotations

import json

import pytest

import eva_variants as ev


def _bar(high: float, low: float, close: float | None = None, ts: int = 0):
    return ev._Bar(ts=ts, high=high, low=low,
                   close=close if close is not None else (high + low) / 2)


def _pos(**kw):
    base = {
        "side": "long",
        "entry": 100.0,
        "stop_loss": 99.0,       # 1.0 of risk
        "take_profits": json.dumps([101.0, 102.0, 103.0]),
        "tps_hit": 0,
        "mfe_r": 0.0,
        "mae_r": 0.0,
        "max_hold_hours": None,
        "opened_at": "2026-09-14T00:00:00Z",
    }
    base.update(kw)
    return base


class TestStopFirst:
    def test_bar_touching_stop_and_target_resolves_stop_first(self):
        """The tie that decides whether the books are honest."""
        res = ev._resolve_one(_pos(), [_bar(high=103.5, low=98.5)])
        assert res["reason"] == "stop"
        assert res["r"] == pytest.approx(-1.0)

    def test_clean_stop_is_minus_one_r(self):
        res = ev._resolve_one(_pos(), [_bar(high=100.2, low=98.0)])
        assert res["reason"] == "stop"
        assert res["r"] == pytest.approx(-1.0)

    def test_short_side_stop(self):
        pos = _pos(side="short", entry=100.0, stop_loss=101.0,
                   take_profits=json.dumps([99.0, 98.0, 97.0]))
        res = ev._resolve_one(pos, [_bar(high=101.5, low=96.0)])
        assert res["reason"] == "stop"
        assert res["r"] == pytest.approx(-1.0)


class TestLadder:
    def test_all_three_rungs_pays_mean_of_rungs(self):
        res = ev._resolve_one(_pos(), [_bar(high=103.5, low=99.9)])
        assert res["reason"] == "target"
        # thirds at +1R, +2R, +3R
        assert res["r"] == pytest.approx(2.0)
        assert res["tps_hit"] == 3

    def test_partial_fill_stays_open_and_records_progress(self):
        res = ev._resolve_one(_pos(), [_bar(high=101.5, low=99.9)])
        assert res.get("_progress") is True
        assert res["tps_hit"] == 1

    def test_stop_after_tp1_is_breakeven_not_minus_one(self):
        """TP1 banks +1/3R and the stop moves to entry, so the floor is +1/3R."""
        bars = [_bar(high=101.5, low=99.9, ts=0), _bar(high=101.0, low=95.0, ts=300)]
        res = ev._resolve_one(_pos(), bars)
        assert res["reason"] == "stop"
        assert res["r"] == pytest.approx(1.0 / 3.0)

    def test_stop_after_tp2_trails_to_first_rung(self):
        bars = [_bar(high=102.5, low=99.9, ts=0), _bar(high=102.0, low=95.0, ts=300)]
        res = ev._resolve_one(_pos(), bars)
        assert res["reason"] == "stop"
        # banked (1+2)/3, remaining third exits at TP1 = +1R
        assert res["r"] == pytest.approx((1.0 + 2.0) / 3.0 + 1.0 / 3.0)


class TestTimeExit:
    def test_time_exit_closes_at_bar_close(self):
        pos = _pos(max_hold_hours=4.0, opened_at="2026-09-14T00:00:00Z")
        # 5h after open, no barrier touched
        late = _bar(high=100.4, low=99.5, close=100.2, ts=1789_000_000)
        pos_open_ts = 1789_000_000 - int(5 * 3600)
        pos["opened_at"] = ev.datetime.fromtimestamp(
            pos_open_ts, tz=ev.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = ev._resolve_one(pos, [late])
        assert res["reason"] == "time_exit"
        assert res["r"] == pytest.approx(0.2)

    def test_no_max_hold_never_time_exits(self):
        res = ev._resolve_one(_pos(), [_bar(high=100.4, low=99.5, ts=9_999_999)])
        assert res.get("_progress") is True

    def test_stop_beats_deadline_on_same_bar(self):
        pos = _pos(max_hold_hours=1.0)
        pos["opened_at"] = ev.datetime.fromtimestamp(
            1789_000_000 - 7200, tz=ev.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = ev._resolve_one(pos, [_bar(high=100.1, low=98.0, ts=1789_000_000)])
        assert res["reason"] == "stop"


class TestExcursions:
    def test_mfe_and_mae_recorded_in_r(self):
        res = ev._resolve_one(_pos(), [_bar(high=100.5, low=99.4)])
        assert res["mfe"] == pytest.approx(0.5)
        assert res["mae"] == pytest.approx(-0.6)

    def test_mae_is_never_positive(self):
        res = ev._resolve_one(_pos(), [_bar(high=100.9, low=100.1)])
        assert res["mae"] <= 0.0


class TestSizing:
    def test_wider_stop_buys_smaller_position_for_equal_dollar_risk(self, tmp_path,
                                                                    monkeypatch):
        """The property that makes the swing arms comparable to control."""
        import config

        monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "t.db")
        tight = ev.open_position(
            ev.DAY, product_id="ETH-USD", side="long", entry=100.0,
            stop_loss=99.0, take_profits=[101.0], entry_source="m1_trigger",
        )
        wide = ev.open_position(
            ev.SWING_MECH, product_id="ETH-USD", side="long", entry=100.0,
            stop_loss=96.0, take_profits=[108.0], entry_source="vision_mirror",
        )
        rows = {r["id"]: r for r in ev.open_positions()}
        assert rows[tight]["qty"] == pytest.approx(4 * rows[wide]["qty"])
        assert rows[tight]["risk_usd"] == pytest.approx(rows[wide]["risk_usd"])

    def test_control_is_not_writable(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "t.db")
        with pytest.raises(ValueError):
            ev.open_position(
                ev.CONTROL, product_id="ETH-USD", side="long", entry=100.0,
                stop_loss=99.0, take_profits=[101.0], entry_source="vision_mirror",
            )

    def test_targets_are_ordered_nearest_first(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "t.db")
        pid = ev.open_position(
            ev.SWING_LLM, product_id="ETH-USD", side="short", entry=100.0,
            stop_loss=101.0, take_profits=[97.0, 99.0, 98.0],
            entry_source="vision_mirror",
        )
        row = {r["id"]: r for r in ev.open_positions()}[pid]
        assert json.loads(row["take_profits"]) == [99.0, 98.0, 97.0]


class TestActionMapping:
    """Eva emits `spot_buy` / `deriv_sell`, never "long" / "short".

    Regression guard for a bug that made both mirrors permanent no-ops: the
    modules mapped only "long"/"buy", so every real suggestion returned None
    and the variant books stayed empty while logging nothing at all. The
    failure mode was silence, which is why it needs a test rather than care.
    """

    @pytest.mark.parametrize("action", ["spot_buy", "deriv_buy", "long", "buy",
                                        "SPOT_BUY", " spot_buy "])
    def test_buy_actions_are_long(self, action):
        assert ev.side_of_action(action) == "long"

    @pytest.mark.parametrize("action", ["spot_sell", "deriv_sell", "short",
                                        "sell", "DERIV_SELL"])
    def test_sell_actions_are_short(self, action):
        assert ev.side_of_action(action) == "short"

    @pytest.mark.parametrize("action", ["no_trade", "", None, "hold",
                                        "spot_hodl"])
    def test_untradeable_actions_map_to_none(self, action):
        assert ev.side_of_action(action) is None

    def test_unknown_action_is_none_rather_than_defaulting_to_short(self):
        """`live_pending.side_of` defaults to short; a variant must not guess."""
        import live_pending

        assert live_pending.side_of("wat") == "short"
        assert ev.side_of_action("wat") is None

    def test_agrees_with_live_pending_on_every_real_action(self):
        import live_pending

        for a in ("spot_buy", "deriv_buy", "spot_sell", "deriv_sell"):
            assert ev.side_of_action(a) == live_pending.side_of(a)

    def test_swing_mirror_opens_on_a_real_spot_buy(self, tmp_path, monkeypatch):
        """End-to-end: the exact call agent.py makes must produce a position."""
        import config
        import eva_swing

        monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "t.db")
        monkeypatch.setattr(eva_swing, "structural_stop",
                            lambda p, s, e: (e * 0.97, "test"))

        class S:
            action = "spot_buy"
            product_id = "ETH-USD"
            entry = 2500.0
            stop_loss = 2475.0

        pid = eva_swing.mirror(S(), cycle_id="T1")
        assert pid is not None
        row = ev.open_positions(ev.SWING_MECH)[0]
        assert row["side"] == "long"
        # Structural stop, not the suggestion's 1% stop.
        assert row["stop_loss"] == pytest.approx(2425.0)

    def test_day_mirror_opens_on_a_real_deriv_sell(self, tmp_path, monkeypatch):
        import config
        import eva_day

        monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "t.db")

        class S:
            action = "deriv_sell"
            product_id = "BTC-USD"
            entry = 60000.0
            stop_loss = 60600.0

        pid = eva_day.mirror_vision_suggestion(S(), cycle_id="T1")
        assert pid is not None
        row = ev.open_positions(ev.DAY)[0]
        assert row["side"] == "short"
        assert row["max_hold_hours"] == pytest.approx(4.0)
        assert row["stop_loss"] > row["entry"]

    def test_no_trade_suggestion_opens_nothing(self, tmp_path, monkeypatch):
        import config
        import eva_day
        import eva_swing

        monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "t.db")

        class S:
            action = "no_trade"
            product_id = "ETH-USD"
            entry = 0.0
            stop_loss = 0.0

        assert eva_swing.mirror(S()) is None
        assert eva_day.mirror_vision_suggestion(S()) is None
        assert ev.open_positions() == []


class TestSwingPlanValidation:
    def test_intermediate_first_target_under_1r_is_accepted(self):
        """The real rejection that had to be fixed.

        Entry 2478, stop 2398 (3.2% structural), TP1 2544.86 = 0.84R aimed at
        Monday High, TP3 2667.01 = 2.36R. Coherent swing geometry; a 1.5R
        first-target floor rejected it and would have starved the book.
        """
        from eva_swing_llm import validate_swing_plan

        ok, why = validate_swing_plan(
            "long", 2478.0, 2398.0, [2544.86, 2567.0, 2667.01]
        )
        assert ok is True, why

    def test_plan_with_no_reach_is_rejected(self):
        from eva_swing_llm import validate_swing_plan

        # risk 3.0: TP1 at 0.67R clears the first floor, TP3 at 1.33R does not
        # reach 2.0R, so the reach check is what must reject this.
        ok, why = validate_swing_plan("long", 100.0, 97.0, [102.0, 103.0, 104.0])
        assert ok is False and "last_target" in why

    def test_stop_too_tight_is_rejected_not_coerced(self):
        from eva_swing_llm import validate_swing_plan

        ok, why = validate_swing_plan("long", 100.0, 99.8, [110.0])
        assert ok is False and "stop_too_tight" in why

    def test_stop_too_wide_is_rejected(self):
        from eva_swing_llm import validate_swing_plan

        ok, why = validate_swing_plan("long", 100.0, 85.0, [150.0])
        assert ok is False and "stop_too_wide" in why

    def test_stop_on_the_wrong_side_is_rejected(self):
        from eva_swing_llm import validate_swing_plan

        assert validate_swing_plan("long", 100.0, 103.0, [110.0])[0] is False
        assert validate_swing_plan("short", 100.0, 97.0, [90.0])[0] is False

    def test_short_side_plan_is_accepted(self):
        from eva_swing_llm import validate_swing_plan

        ok, why = validate_swing_plan("short", 100.0, 102.0, [99.0, 97.0, 95.0])
        assert ok is True, why


class TestControlBookCollapse:
    """The control reader must count positions, not ladder legs.

    `paper.get_closed_trades` returns one row per scale-out, so a 3-target
    winner appears three times. Counting legs inflates the win rate by exactly
    the trades that worked best, which would make control look better than the
    variants for a purely structural reason.
    """

    def _three_leg_winner(self):
        # entry 100, stop 95 -> 5.0 risk/unit. Three thirds at 105/110/115.
        return [
            {"open_cycle_id": "C1", "product_id": "ETH-USD", "side": "long",
             "entry": 100.0, "exit": 105.0, "qty": 1.0,
             "realized_pnl_usd": 5.0, "opened_at": "2026-09-14T00:00:00Z",
             "closed_at": "2026-09-14T04:00:00Z", "close_reason": "take_profit"},
            {"open_cycle_id": "C1", "product_id": "ETH-USD", "side": "long",
             "entry": 100.0, "exit": 110.0, "qty": 1.0,
             "realized_pnl_usd": 10.0, "opened_at": "2026-09-14T00:00:00Z",
             "closed_at": "2026-09-14T08:00:00Z", "close_reason": "take_profit"},
            {"open_cycle_id": "C1", "product_id": "ETH-USD", "side": "long",
             "entry": 100.0, "exit": 115.0, "qty": 1.0,
             "realized_pnl_usd": 15.0, "opened_at": "2026-09-14T00:00:00Z",
             "closed_at": "2026-09-14T12:00:00Z", "close_reason": "take_profit"},
        ]

    def _patch(self, monkeypatch, legs):
        import eva_variants_bridge as evb
        import paper

        monkeypatch.setattr(paper, "get_closed_trades", lambda limit=10: legs)
        monkeypatch.setattr(
            evb, "_control_stops",
            lambda: {"C1": {"stop_loss": 95.0, "avg_entry": 100.0,
                            "tps_hit": 3, "mfe_pct": 15.0}},
        )
        return evb

    def test_three_ladder_legs_collapse_to_one_position(self, monkeypatch):
        evb = self._patch(monkeypatch, self._three_leg_winner())
        rows = evb.control_positions(since="2000-01-01")
        assert len(rows) == 1

    def test_collapsed_position_sums_pnl_and_averages_entry(self, monkeypatch):
        evb = self._patch(monkeypatch, self._three_leg_winner())
        row = evb.control_positions(since="2000-01-01")[0]
        assert row["realized_pnl_usd"] == pytest.approx(30.0)
        assert row["entry"] == pytest.approx(100.0)
        # 30 USD over 3 units at 5.0 risk/unit = +2R
        assert row["realized_r"] == pytest.approx(2.0)

    def test_hold_spans_first_open_to_last_close(self, monkeypatch):
        evb = self._patch(monkeypatch, self._three_leg_winner())
        row = evb.control_positions(since="2000-01-01")[0]
        assert row["hold_h"] == pytest.approx(12.0)

    def test_win_rate_counts_one_trade_not_three(self, monkeypatch):
        evb = self._patch(monkeypatch, self._three_leg_winner())
        s = evb.control_summary(since="2000-01-01")
        assert s["n_closed"] == 1
        assert s["win_rate"] == pytest.approx(1.0)
        assert s["mean_r"] == pytest.approx(2.0)

    def test_position_with_no_recorded_stop_is_skipped_not_counted_as_zero(
        self, monkeypatch
    ):
        """A missing stop makes R undefined; inventing one would fake a result."""
        import eva_variants_bridge as evb
        import paper

        monkeypatch.setattr(paper, "get_closed_trades",
                            lambda limit=10: self._three_leg_winner())
        monkeypatch.setattr(evb, "_control_stops", lambda: {})
        assert evb.control_positions(since="2000-01-01") == []

    def test_epoch_filter_excludes_pre_experiment_trades(self, monkeypatch):
        evb = self._patch(monkeypatch, self._three_leg_winner())
        assert evb.control_positions(since="2026-10-01") == []
        assert len(evb.control_positions(since="2026-09-01")) == 1


class TestPayload:
    def test_all_four_books_present_with_exactly_one_live(self, tmp_path,
                                                          monkeypatch):
        import bot_config
        import config
        import eva_variants_bridge as evb

        monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "t.db")
        monkeypatch.setattr(bot_config, "EVA_VARIANTS_ENABLED", True)
        monkeypatch.setattr(bot_config, "EVA_LIVE_VARIANT", "control")
        monkeypatch.setattr(evb, "_control_stops", lambda: {})

        payload = evb.performance_payload()
        assert payload["available"] is True
        names = [b["variant"] for b in payload["books"]]
        assert names == list(ev.VARIANTS)
        live = [b for b in payload["books"] if b["mode"] == "live"]
        assert len(live) == 1 and live[0]["variant"] == "control"

    def test_disabled_flag_yields_unavailable(self, monkeypatch):
        import bot_config
        import eva_variants_bridge as evb

        monkeypatch.setattr(bot_config, "EVA_VARIANTS_ENABLED", False)
        assert evb.performance_payload() == {"available": False}


class TestCooldown:
    def test_has_open_is_scoped_by_variant_product_and_side(self, tmp_path,
                                                            monkeypatch):
        import config

        monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "t.db")
        ev.open_position(
            ev.DAY, product_id="ETH-USD", side="long", entry=100.0,
            stop_loss=99.0, take_profits=[101.0], entry_source="m1_trigger",
        )
        assert ev.has_open(ev.DAY, "ETH-USD", "long") is True
        assert ev.has_open(ev.DAY, "ETH-USD", "short") is False
        assert ev.has_open(ev.DAY, "BTC-USD", "long") is False
        assert ev.has_open(ev.SWING_MECH, "ETH-USD", "long") is False

    def test_skips_are_counted(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "t.db")
        ev.record_skip(ev.DAY, reason="cooldown", product_id="ETH-USD",
                       side="long", trigger_name="fvg")
        ev.record_skip(ev.DAY, reason="stance_gate", product_id="BTC-USD")
        assert ev.skip_counts(ev.DAY) == 2
        assert ev.skip_counts(ev.SWING_MECH) == 0
