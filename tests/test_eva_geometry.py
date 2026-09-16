"""eva_geometry — the vol-conditioned floor/cap rule, pure-function surface.

The measured rule (analysis/_q0916_dynamic_geometry.py, 2026-09-16):
stop distance floored at EVA_GEOM_STOP_FLOOR_ATR x ATR24 (never tightened,
hard 4.5% ceiling), TP rung k capped at k x EVA_GEOM_TP_CAP_ATR x ATR24
(never widened). These tests pin the arithmetic and the refuse-to-act paths.
"""
import pytest

import eva_geometry as eg


# Defaults assumed by the cases: floor 7 x ATR, cap 8 x ATR, max stop 4.5%.
ATR = 0.20  # ATR24 = 0.20% of price


class TestStopFloor:
    def test_tight_stop_is_widened_to_the_floor(self):
        # entry 2000, stop 1990 = 0.5%; floor = 7 x 0.20% = 1.4% -> 1972
        stop, _tps, info = eg.condition_levels(
            "long", 2000.0, 1990.0, [2100.0], ATR)
        assert info["applied"] and info["stop_widened"]
        assert stop == pytest.approx(2000.0 * (1 - 0.014))

    def test_wide_stop_is_never_tightened(self):
        # 3% recorded stop > 1.4% floor -> untouched
        stop, _tps, info = eg.condition_levels(
            "long", 2000.0, 1940.0, [2100.0], ATR)
        assert stop == pytest.approx(1940.0)
        assert not info["stop_widened"]

    def test_floor_respects_the_hard_ceiling(self):
        # ATR 1.0% -> raw floor 7% but ceiling is 4.5%
        stop, _tps, _ = eg.condition_levels(
            "long", 2000.0, 1996.0, [2100.0], 1.0)
        assert stop == pytest.approx(2000.0 * (1 - 0.045))

    def test_short_side_mirrors(self):
        stop, tps, _ = eg.condition_levels(
            "short", 2000.0, 2010.0, [1900.0], ATR)
        assert stop == pytest.approx(2000.0 * 1.014)
        assert tps[0] < 2000.0


class TestTargetCaps:
    def test_far_rungs_are_capped_at_rank_ladder(self):
        # caps: 1.6%, 3.2%, 4.8% of 2000 = 32, 64, 96 away
        _stop, tps, info = eg.condition_levels(
            "long", 2000.0, 1980.0, [2040.0, 2100.0, 2200.0], ATR)
        assert tps == pytest.approx([2032.0, 2064.0, 2096.0])
        assert info["tps_capped"] == 3

    def test_near_targets_are_never_widened(self):
        _stop, tps, info = eg.condition_levels(
            "long", 2000.0, 1980.0, [2010.0, 2020.0, 2030.0], ATR)
        assert tps == pytest.approx([2010.0, 2020.0, 2030.0])
        assert info["tps_capped"] == 0

    def test_ladder_stays_monotone_after_mixed_capping(self):
        # TP1 beyond its cap, TP2 inside its own -> ladder must not invert
        _stop, tps, _ = eg.condition_levels(
            "long", 2000.0, 1980.0, [2050.0, 2055.0], ATR)
        assert tps[0] <= tps[1]
        assert tps[0] == pytest.approx(2032.0)  # capped at 1 x 8 x ATR


class TestRefusalPaths:
    def test_none_atr_returns_inputs_untouched(self):
        stop, tps, info = eg.condition_levels(
            "long", 2000.0, 1990.0, [2100.0], None)
        assert (stop, tps) == (1990.0, [2100.0])
        assert not info["applied"]

    def test_no_targets_returns_untouched(self):
        stop, tps, info = eg.condition_levels(
            "long", 2000.0, 1990.0, [], ATR)
        assert not info["applied"]
        assert stop == 1990.0 and tps == []

    def test_apply_skips_no_trade(self):
        class S:
            action = "no_trade"
            entry = None
            stop_loss = None
            take_profits = []
            product_id = "ETH-USD"

        assert eg.apply_to_suggestion(S()) is None

    def test_apply_uses_fetched_atr_and_mutates(self, monkeypatch):
        monkeypatch.setattr(eg, "atr24_pct", lambda p: ATR)

        class S:
            action = "spot_buy"
            entry = 2000.0
            stop_loss = 1990.0
            take_profits = [2200.0]
            product_id = "ETH-USD"

        s = S()
        info = eg.apply_to_suggestion(s)
        assert info["applied"]
        assert s.stop_loss == pytest.approx(2000.0 * (1 - 0.014))
        assert s.take_profits[0] == pytest.approx(2032.0)
