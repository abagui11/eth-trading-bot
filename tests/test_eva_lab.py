"""The Eva Lab strategy funnel: every book, staged, honestly normalized.

The funnel's one job is to show where each strategy sits in the approval
process without laundering a leaderboard into a promotion. These tests pin
the parts that could silently lie:

* stage comes from the operator registry, defaulting to Stage 1 — never
  from performance;
* Kalshi dollar books gain a percent-of-seed read, HQ books keep R, the
  mill stays in percent-of-notional — units are never blended;
* one dead ledger never blanks the other families.
"""

from __future__ import annotations

from unittest import mock

import pytest

import eva_variants_bridge
import kalshi_bridge
import trade_ideas_bridge
from dashboard import eva_lab


def _hq_payload() -> dict:
    return {
        "available": True,
        "books": [
            {
                "variant": "control", "label": "Control (live Eva HQ)",
                "blurb": "baseline", "mode": "live",
                "n_closed": 13, "n_open": 1, "win_rate": 0.38,
                "mean_r": -0.11, "sum_r": -1.43, "pnl_usd": -40.60,
                "median_hold_h": 10.1, "stopped_n": 10,
                "stopped_then_paid": 7, "skips": 0,
            },
            {
                "variant": "eva_day", "label": "Day — fast ICT",
                "blurb": "4h close", "mode": "live_mirror",
                "n_closed": 13, "n_open": 0, "win_rate": 0.62,
                "mean_r": 0.25, "sum_r": 3.28, "pnl_usd": 32.78,
                "median_hold_h": 0.3, "stopped_n": 2,
                "stopped_then_paid": 1, "skips": 4,
            },
        ],
    }


def _kalshi_payload() -> dict:
    return {
        "available": True,
        "bots": [
            {
                "bot_id": "eva_wick", "label": "EVA wick", "blurb": "band",
                "mode": "live", "starting_usd": 246.75,
                "epoch_pnl_usd": 114.62, "closed": 790, "open": 2,
                "win_rate": 0.75,
            },
        ],
        "altcoins": {
            "available": True,
            "bots": [
                {
                    "bot_id": "eva_wick_sol", "label": "SOL",
                    "series": "KXSOL15M", "mode": "paper",
                    "starting_usd": 50.0, "epoch_pnl_usd": -1.25,
                    "closed": 3, "open": 1, "win_rate": 0.33,
                },
            ],
        },
        "hourly": {
            "available": True,
            "bots": [
                {
                    "bot_id": "eva_wick_1h_ladder",
                    "label": "1h ladder · 4/2/1 once per hour",
                    "blurb": "rungs past spot on wick fires",
                    "series": "KXBTCD · KXETHD", "mode": "paper",
                    "starting_usd": 246.75, "epoch_pnl_usd": 3.10,
                    "closed": 6, "open": 3, "win_rate": 0.5,
                },
            ],
        },
        "cross": {
            "available": True,
            "bots": [
                {
                    "bot_id": "eva_wick_btc_xrp", "label": "BTC→XRP",
                    "blurb": "BTC wick fire + EVA agreement, traded on XRP",
                    "series": "KXXRP15M", "signal": "BTC", "coin": "XRP",
                    "mode": "paper", "starting_usd": 225.0,
                    "epoch_pnl_usd": -0.80, "closed": 4, "open": 0,
                    "win_rate": 0.25,
                },
            ],
        },
    }


def _mill_payload() -> dict:
    return {
        "available": True,
        "summary": {"open": 4, "closed": 96, "win_rate": 0.5,
                    "pnl_pct_sum": -3.4},
        "trades": [
            {"product_id": "ETH-USD"},
            {"product_id": "BTC-USD"},
            {"product_id": "ETH-USD"},
        ],
    }


@pytest.fixture()
def patched_sources(monkeypatch):
    monkeypatch.setattr(
        eva_variants_bridge, "performance_payload",
        lambda limit=1: _hq_payload())
    monkeypatch.setattr(
        kalshi_bridge, "performance_payload",
        lambda limit=1: _kalshi_payload())
    monkeypatch.setattr(
        trade_ideas_bridge, "volume_book_payload",
        lambda limit=50: _mill_payload())
    # Keep the funnel hermetic: the P(>0) series readers hit real ledgers,
    # which don't exist here. Empty series -> p_edge None, cache fresh.
    monkeypatch.setattr(eva_lab, "_pedge_cache", {})
    monkeypatch.setattr(eva_lab, "_hq_series", lambda *a, **k: [])
    monkeypatch.setattr(eva_lab, "_kalshi_series", lambda *a, **k: [])
    monkeypatch.setattr(eva_lab, "_mill_series", lambda *a, **k: [])


def _books(payload: dict) -> dict[str, dict]:
    return {
        b["key"]: b
        for st in payload["stages"]
        for b in st["books"]
    }


class TestFunnelComposition:
    def test_every_family_is_represented(self, patched_sources):
        books = _books(eva_lab.funnel_payload())
        assert set(books) == {
            "hq:control", "hq:eva_day",
            "kalshi:eva_wick", "kalshi:eva_wick_sol",
            "kalshi:eva_wick_1h_ladder",
            "kalshi:eva_wick_btc_xrp",
            "mill:ideas",
        }

    def test_everything_defaults_to_stage_one(self, patched_sources,
                                              monkeypatch):
        monkeypatch.setattr(eva_lab, "STRATEGY_STAGE", {})
        payload = eva_lab.funnel_payload()
        by_stage = {st["stage"]: st["books"] for st in payload["stages"]}
        assert len(by_stage[1]) == 7
        assert by_stage[2] == []
        assert by_stage[3] == []

    def test_live_wick_books_sit_in_stage_two(self, patched_sources):
        """2026-10-01 promotions: BTC/ETH wick and the SOL clone."""
        assert eva_lab.STRATEGY_STAGE["kalshi:eva_wick"] == 2
        assert eva_lab.STRATEGY_STAGE["kalshi:eva_wick_sol"] == 2
        payload = eva_lab.funnel_payload()
        stage2 = {b["key"] for b in payload["stages"][1]["books"]}
        assert "kalshi:eva_wick" in stage2

    def test_all_three_stages_always_render(self, patched_sources):
        payload = eva_lab.funnel_payload()
        assert [st["stage"] for st in payload["stages"]] == [1, 2, 3]
        for st in payload["stages"]:
            assert st["name"] and st["tagline"]
            assert st["criteria_label"] and len(st["criteria"]) >= 2

    def test_graduation_bar_states_r_and_edge_probability(self,
                                                          patched_sources):
        """The user-facing bars must spell out the P(edge>0) rules.

        Amended 2026-10-01: the prereg §4 statistical bar moved from the
        Stage 2 gate to the Stage 3 gate — it must still be stated in full.
        """
        payload = eva_lab.funnel_payload()
        entry = " ".join(payload["stages"][0]["criteria"])
        assert "P(edge>0) ≥ 0.85" in entry
        assert "500 closed" in entry and "10 trading days" in entry
        assert "sponsor" in entry
        approve = " ".join(payload["stages"][1]["criteria"])
        assert "60 closed live positions" in approve
        assert "95% CI" in approve
        assert "P(edge>0)" in approve and "0.975" in approve
        assert "random entries" in approve  # placebo
        assert "Demotion" in approve

    def test_stage_registry_moves_a_book(self, patched_sources, monkeypatch):
        monkeypatch.setattr(
            eva_lab, "STRATEGY_STAGE", {"kalshi:eva_wick": 2})
        payload = eva_lab.funnel_payload()
        by_stage = {st["stage"]: [b["key"] for b in st["books"]]
                    for st in payload["stages"]}
        assert by_stage[2] == ["kalshi:eva_wick"]
        assert "kalshi:eva_wick" not in by_stage[1]


class TestUnitHonesty:
    """R, $ and % must never blend across book families."""

    def test_hq_books_keep_r_and_dollars(self, patched_sources):
        b = _books(eva_lab.funnel_payload())["hq:eva_day"]
        assert b["mean_r"] == 0.25
        assert b["sum_r"] == 3.28
        assert b["pnl_usd"] == 32.78
        assert b["pnl_pct"] is None  # no sleeve base — a % would be invented
        assert b["coins"] == ["BTC", "ETH"]

    def test_kalshi_pct_is_epoch_pnl_over_seed(self, patched_sources):
        b = _books(eva_lab.funnel_payload())["kalshi:eva_wick"]
        assert b["pnl_usd"] == 114.62
        assert b["pnl_pct"] == pytest.approx(114.62 / 246.75 * 100, abs=0.01)
        assert b["mean_r"] is None and b["sum_r"] is None

    def test_altcoin_clone_carries_its_own_coin(self, patched_sources):
        b = _books(eva_lab.funnel_payload())["kalshi:eva_wick_sol"]
        assert b["coins"] == ["SOL"]
        assert b["label"] == "SOL wick clone"
        assert b["mode"] == "paper"

    def test_hourly_piggyback_is_its_own_family(self, patched_sources):
        b = _books(eva_lab.funnel_payload())["kalshi:eva_wick_1h_ladder"]
        assert b["family"] == "Kalshi 1h · wick piggyback"
        assert b["coins"] == ["BTC", "ETH"]
        assert b["mode"] == "paper"
        assert b["pnl_pct"] == pytest.approx(3.10 / 246.75 * 100, abs=0.01)

    def test_cross_wick_chip_is_the_traded_coin(self, patched_sources):
        b = _books(eva_lab.funnel_payload())["kalshi:eva_wick_btc_xrp"]
        assert b["family"] == "Kalshi 15m · cross wick"
        assert b["coins"] == ["XRP"]  # the book trades XRP; BTC is the signal
        assert b["label"] == "BTC→XRP"
        assert b["mode"] == "paper"

    def test_mill_is_percent_only(self, patched_sources):
        b = _books(eva_lab.funnel_payload())["mill:ideas"]
        assert b["pnl_usd"] is None
        assert b["pnl_pct"] == -3.4
        assert b["coins"] == ["BTC", "ETH"]
        # 2026-10-01: mode follows the fill switches. All off (today's
        # posture) → paper; any on → LIVE* (a subset of the book fills).
        assert b["mode"] == "paper"
        assert b["mode_note"] is None

    def test_mill_mode_follows_the_fill_switches(self, patched_sources):
        import bot_config
        from unittest.mock import patch as _patch

        with _patch.object(bot_config, "LIVE_MILL_AUTO_FILL_ENABLED", True):
            b = _books(eva_lab.funnel_payload())["mill:ideas"]
        assert b["mode"] == "live_mirror"
        assert b["mode_note"]

    def test_live_mirror_note_survives_to_the_row(self, patched_sources):
        b = _books(eva_lab.funnel_payload())["hq:eva_day"]
        assert b["mode"] == "live_mirror"
        assert "NOT a promotion" in b["mode_note"]


class TestPEdge:
    """The P(>0) column: day-clustered bootstrap, honest about thin samples."""

    def test_every_row_carries_the_key(self, patched_sources):
        for b in _books(eva_lab.funnel_payload()).values():
            assert "p_edge" in b

    def test_under_two_days_is_none_not_a_number(self):
        rows = [("2026-09-30T10:00:00Z", 1.0), ("2026-09-30T11:00:00Z", 2.0)]
        assert eva_lab._bootstrap_p_edge(rows, key="x") is None

    def test_all_positive_days_reads_one(self):
        rows = [(f"2026-09-{d:02d}T10:00:00Z", 1.0) for d in (27, 28, 29, 30)]
        assert eva_lab._bootstrap_p_edge(rows, key="x") == 1.0

    def test_deterministic_and_unit_invariant(self):
        rows = [("2026-09-28T10:00:00Z", 3.0), ("2026-09-29T10:00:00Z", -1.0),
                ("2026-09-30T10:00:00Z", -1.0), ("2026-09-30T12:00:00Z", 2.0)]
        p1 = eva_lab._bootstrap_p_edge(rows, key="k")
        p2 = eva_lab._bootstrap_p_edge(rows, key="k")
        scaled = [(t, v * 1000.0) for t, v in rows]
        p3 = eva_lab._bootstrap_p_edge(scaled, key="k")
        assert p1 == p2 == p3  # same seed, same sign pattern -> identical
        assert 0.0 < p1 < 1.0  # mixed days must not read as certainty

    def test_flows_into_a_kalshi_row(self, patched_sources, monkeypatch):
        series = [(f"2026-09-{d:02d}T10:00:00Z", 1.0) for d in (28, 29, 30)]
        monkeypatch.setattr(
            eva_lab, "_kalshi_series", lambda bot_id, since: series)
        b = _books(eva_lab.funnel_payload())["kalshi:eva_wick"]
        assert b["p_edge"] == 1.0

    def test_series_failure_degrades_only_the_column(self, patched_sources,
                                                     monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("ledger gone")

        monkeypatch.setattr(eva_lab, "_kalshi_series", boom)
        b = _books(eva_lab.funnel_payload())["kalshi:eva_wick"]
        assert b["p_edge"] is None  # column degrades
        assert b["pnl_usd"] == 114.62  # row survives

    def test_cache_skips_recompute_until_n_moves(self, patched_sources,
                                                 monkeypatch):
        calls = {"n": 0}

        def counting_series(bot_id, since):
            calls["n"] += 1
            return [(f"2026-09-{d:02d}T10:00:00Z", 1.0) for d in (29, 30)]

        monkeypatch.setattr(eva_lab, "_kalshi_series", counting_series)
        eva_lab.funnel_payload()
        eva_lab.funnel_payload()  # same closed counts -> cache hit
        assert calls["n"] == 4  # one per kalshi book, once each


class TestFailureIsolation:
    def test_one_dead_ledger_keeps_the_others(self, patched_sources,
                                              monkeypatch):
        monkeypatch.setattr(
            kalshi_bridge, "performance_payload",
            mock.Mock(side_effect=RuntimeError("ledger gone")))
        books = _books(eva_lab.funnel_payload())
        assert "hq:control" in books
        assert "mill:ideas" in books
        assert not any(k.startswith("kalshi:") for k in books)

    def test_unavailable_sources_yield_empty_stages(self, monkeypatch):
        monkeypatch.setattr(
            eva_variants_bridge, "performance_payload",
            lambda limit=1: {"available": False})
        monkeypatch.setattr(
            kalshi_bridge, "performance_payload", lambda limit=1: None)
        monkeypatch.setattr(
            trade_ideas_bridge, "volume_book_payload", lambda limit=50: None)
        payload = eva_lab.funnel_payload()
        assert payload["available"] is True
        assert payload["n_books"] == 0
        assert all(st["books"] == [] for st in payload["stages"])
