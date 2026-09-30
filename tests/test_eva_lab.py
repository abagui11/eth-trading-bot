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
            "mill:ideas",
        }

    def test_everything_defaults_to_stage_one(self, patched_sources):
        payload = eva_lab.funnel_payload()
        by_stage = {st["stage"]: st["books"] for st in payload["stages"]}
        assert len(by_stage[1]) == 5
        assert by_stage[2] == []
        assert by_stage[3] == []

    def test_all_three_stages_always_render(self, patched_sources):
        payload = eva_lab.funnel_payload()
        assert [st["stage"] for st in payload["stages"]] == [1, 2, 3]
        for st in payload["stages"]:
            assert st["name"] and st["tagline"]
            assert st["criteria_label"] and len(st["criteria"]) >= 2

    def test_graduation_bar_states_r_and_edge_probability(self,
                                                          patched_sources):
        """The user-facing bar must spell out the R and P(edge>0) rules."""
        payload = eva_lab.funnel_payload()
        stage1 = payload["stages"][0]
        text = " ".join(stage1["criteria"])
        assert "60 closed positions" in text
        assert "mean R" in text
        assert "95% CI" in text
        assert "P(edge>0)" in text and "0.975" in text
        assert "random entries" in text  # placebo
        assert len(stage1["criteria"]) == 5  # prereg §4: all five, named

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

    def test_mill_is_percent_only(self, patched_sources):
        b = _books(eva_lab.funnel_payload())["mill:ideas"]
        assert b["pnl_usd"] is None
        assert b["pnl_pct"] == -3.4
        assert b["coins"] == ["BTC", "ETH"]
        # Live fills exist but are a subset — badge must say LIVE*, not LIVE.
        assert b["mode"] == "live_mirror"
        assert b["mode_note"]

    def test_live_mirror_note_survives_to_the_row(self, patched_sources):
        b = _books(eva_lab.funnel_payload())["hq:eva_day"]
        assert b["mode"] == "live_mirror"
        assert "NOT a promotion" in b["mode_note"]


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
