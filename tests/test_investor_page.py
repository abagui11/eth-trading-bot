"""Private investor view: access gate, health factor, and ladder reporting."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class InvestorPageTests(unittest.TestCase):
    """Renders against a real ledger so the payload path is exercised end to end."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmpdir.name)
        self._charts = root / "charts"
        self._charts.mkdir()
        db = root / "ledger.db"
        db.write_bytes(b"")

        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "CHARTS_DIR", self._charts),
            patch.object(config, "ROOT_DIR", root),
            patch.object(config, "INVESTOR_ACCESS_TOKEN", None),
            patch(
                "dashboard.data.research.get_spot_prices",
                return_value={"ETH-USD": 2000.0, "BTC-USD": 60000.0},
            ),
        ]
        for p in self._patches:
            p.start()

        import live_ledger
        from dashboard import data
        from dashboard.app import create_app
        from fastapi.testclient import TestClient

        data.reset_spot_cache()
        live_ledger.init_db()
        self.live_ledger = live_ledger

        # $4,200 of exposure: 2.1 ETH marked at $2,000 against $1,400 of
        # collateral, which is the 3x / ~33% case the page was specced from.
        self.trade_id = live_ledger.record_open(
            cycle_id=None,
            source="hq",
            product_id="ETH-USD",
            instrument="ETH-20DEC30-CDE",
            side="long",
            qty=2.1,
            entry=2000.0,
            stop_loss=1900.0,
            take_profits_json=json.dumps([2100.0, 2200.0, 2300.0]),
            order_id="o1",
            stop_order_id="s1",
        )
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        for p in reversed(self._patches):
            p.stop()
        self._tmpdir.cleanup()

    def test_health_factor_is_collateral_over_total_position_size(self) -> None:
        """$2k Eva sleeve behind $4.2k of exposure is 2.1x / ~48%."""
        from dashboard.investor import build_investor_payload

        health = build_investor_payload(include_paper=False)["health"]

        self.assertEqual(health["equity_usd"], 2000.0)
        self.assertEqual(health["gross_notional_usd"], 4200.0)
        self.assertAlmostEqual(health["health_pct"], 47.6, places=1)
        self.assertAlmostEqual(health["leverage_x"], 2.1, places=2)
        self.assertEqual(health["band"], "good")

    def test_mill_clips_do_not_appear_on_the_page(self) -> None:
        """Mill shares the Coinbase account but is a different product."""
        self.live_ledger.record_open(
            cycle_id=None,
            source="mill",
            product_id="ETH-USD",
            instrument="ETH-20DEC30-CDE",
            side="long",
            qty=0.1,
            entry=2000.0,
            stop_loss=1900.0,
            take_profits_json="[]",
            order_id="mill-1",
            stop_order_id="mill-s1",
        )
        from dashboard.investor import build_investor_payload

        payload = build_investor_payload(include_paper=False)
        html = self.client.get("/investors").text

        self.assertEqual(len(payload["open_trades"]), 1)
        self.assertEqual(payload["open_trades"][0]["source"], "hq")
        self.assertNotIn("Mill realized", html)
        self.assertNotIn("Trade mill", html)

    def test_zero_coinbase_liq_price_renders_as_na(self) -> None:
        from dashboard.investor import _parse_liq_price

        self.assertIsNone(_parse_liq_price({"raw": {"liquidation_price": "0"}}))
        self.assertEqual(
            _parse_liq_price({"raw": {"liquidation_price": "1800.5"}}), 1800.5
        )

    def test_flat_book_reports_no_exposure_rather_than_dividing_by_zero(self) -> None:
        self.live_ledger.record_close(
            self.trade_id, exit_price=2100.0, pnl_usd=210.0, close_reason="take_profit"
        )
        from dashboard.investor import build_investor_payload

        health = build_investor_payload(include_paper=False)["health"]

        self.assertFalse(health["has_exposure"])
        self.assertIsNone(health["health_pct"])
        self.assertEqual(health["band"], "none")

    def test_page_leads_with_the_four_headline_numbers(self) -> None:
        html = self.client.get("/investors").text

        self.assertIn("Portfolio value", html)
        self.assertIn("Realized 2026 (YTD)", html)
        self.assertIn("Realized today", html)
        self.assertIn("Unrealized (open)", html)
        self.assertIn("Health factor", html)
        self.assertIn("Open positions (1)", html)
        self.assertNotIn("Exchange account", html)
        self.assertNotIn("Account equity", html)

    def test_page_is_not_indexable(self) -> None:
        """A forwarded private link must never end up in a search result."""
        html = self.client.get("/investors").text
        self.assertIn('content="noindex,nofollow"', html)

    def test_open_position_shows_entry_mark_size_and_liq(self) -> None:
        html = self.client.get("/investors").text

        self.assertIn("$2000.00", html)  # entry
        self.assertIn("mark $2000.00", html)
        self.assertIn("2.10 ETH", html)
        self.assertIn("liq n/a", html)
        self.assertIn("Unrealized", html)

    def test_untouched_ladder_renders_every_target_unhit(self) -> None:
        html = self.client.get("/investors").text

        self.assertIn(">TP1</span>", html)
        self.assertIn(">TP3</span>", html)
        self.assertNotIn("TP1 ✓", html)

    def test_banked_target_marks_the_rung_hit_and_shows_the_trail(self) -> None:
        """A TP hit is only a booked exit leg — nothing sets a tp1 flag."""
        self.live_ledger.record_partial_exit(
            self.trade_id,
            exit_qty=0.7,
            exit_price=2100.0,
            pnl_usd=70.0,
            order_id="tp1",
            reason="take_profit",
        )
        self.live_ledger.set_stop_loss(self.trade_id, 2000.0)

        from dashboard import data

        rows = data.enrich_live_trades(self.live_ledger.get_open_trades())
        ladder = rows[0]["tp_progress"]

        self.assertTrue(ladder[0]["hit"])
        self.assertEqual(ladder[0]["pnl_usd"], 70.0)
        self.assertFalse(ladder[1]["hit"])
        self.assertEqual(rows[0]["tps_hit"], 1)
        self.assertEqual(rows[0]["realized_pnl_usd"], 70.0)

        # The trail is only visible because the opening stop was recorded at
        # fill time; stop_loss itself has already been overwritten.
        self.assertTrue(rows[0]["stop_state"]["trailed"])
        self.assertEqual(rows[0]["stop_state"]["initial"], 1900.0)
        self.assertEqual(rows[0]["stop_state"]["label"], "breakeven")

        html = self.client.get("/investors").text
        self.assertIn("TP1 ✓", html)
        self.assertIn("SL breakeven", html)

    def test_realized_by_day_counts_scale_outs_on_still_open_trades(self) -> None:
        """Banked profit was invisible until the runner finally closed."""
        self.live_ledger.record_partial_exit(
            self.trade_id,
            exit_qty=0.7,
            exit_price=2100.0,
            pnl_usd=70.0,
            order_id="tp1",
            reason="take_profit",
        )
        days = self.live_ledger.get_realized_by_day()

        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["realized_pnl_usd"], 70.0)

    def test_daily_totals_reconcile_with_live_performance(self) -> None:
        self.live_ledger.record_partial_exit(
            self.trade_id,
            exit_qty=0.7,
            exit_price=2100.0,
            pnl_usd=70.0,
            order_id="tp1",
            reason="take_profit",
        )
        self.live_ledger.record_close(
            self.trade_id, exit_price=2200.0, pnl_usd=280.0, close_reason="take_profit"
        )
        days = self.live_ledger.get_realized_by_day()
        total = sum(d["realized_pnl_usd"] for d in days)

        self.assertEqual(
            total, self.live_ledger.get_live_performance()["total_pnl_usd"]
        )

    def test_archived_paper_history_is_included(self) -> None:
        html = self.client.get("/investors").text
        self.assertIn("Paper book v2 · simulated", html)


class InvestorAccessTests(unittest.TestCase):
    """The link is meant to be forwarded, so the token has to survive a share."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmpdir.name)
        charts = root / "charts"
        charts.mkdir()
        db = root / "ledger.db"
        db.write_bytes(b"")

        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "CHARTS_DIR", charts),
            patch.object(config, "ROOT_DIR", root),
            patch.object(config, "INVESTOR_ACCESS_TOKEN", "s3cret"),
            patch(
                "dashboard.data.research.get_spot_prices",
                return_value={"ETH-USD": 2000.0, "BTC-USD": 60000.0},
            ),
        ]
        for p in self._patches:
            p.start()

        from dashboard import data
        from dashboard.app import create_app
        from fastapi.testclient import TestClient

        data.reset_spot_cache()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        for p in reversed(self._patches):
            p.stop()
        self._tmpdir.cleanup()

    def test_missing_token_looks_like_the_page_does_not_exist(self) -> None:
        """404 not 401 — a wrong guess should not confirm there is a page here."""
        self.assertEqual(self.client.get("/investors").status_code, 404)

    def test_wrong_token_is_rejected(self) -> None:
        self.assertEqual(self.client.get("/investors?k=nope").status_code, 404)

    def test_correct_token_opens_the_page_and_leaves_a_cookie(self) -> None:
        res = self.client.get("/investors?k=s3cret")

        self.assertEqual(res.status_code, 200)
        self.assertIn("investor_access", res.cookies)
        # Cookie carries the session, so a reload without the query still works.
        self.assertEqual(self.client.get("/investors").status_code, 200)

    def test_snapshot_api_is_gated_too(self) -> None:
        fresh = self.client
        self.assertEqual(fresh.get("/api/investors/snapshot").status_code, 404)
        self.assertEqual(
            fresh.get("/api/investors/snapshot?k=s3cret").status_code, 200
        )

    def test_configured_capital_stands_in_when_the_exchange_is_unreachable(self) -> None:
        import bot_config
        from dashboard.investor import build_investor_payload

        payload = build_investor_payload(include_paper=False)

        self.assertEqual(payload["portfolio"]["capital_base_basis"], "configured")
        self.assertEqual(
            payload["portfolio"]["capital_base_usd"],
            bot_config.LIVE_HQ_EQUITY_USD,
        )


class StopStateTests(unittest.TestCase):
    """stop_loss is overwritten in place, so 'trailed' needs the original."""

    def test_untrailed_stop_reports_its_opening_level(self) -> None:
        from dashboard.data import build_stop_state

        state = build_stop_state("long", 2000.0, 1900.0, 1900.0)

        self.assertFalse(state["trailed"])
        self.assertEqual(state["label"], "initial")
        self.assertFalse(state["at_breakeven"])

    def test_stop_moved_to_entry_reads_as_breakeven(self) -> None:
        from dashboard.data import build_stop_state

        state = build_stop_state("long", 2000.0, 2000.0, 1900.0)

        self.assertTrue(state["trailed"])
        self.assertEqual(state["label"], "breakeven")
        self.assertTrue(state["at_breakeven"])

    def test_stop_beyond_entry_reports_the_profit_it_locks(self) -> None:
        from dashboard.data import build_stop_state

        state = build_stop_state("long", 2000.0, 2100.0, 1900.0, qty_open=0.5)

        self.assertEqual(state["label"], "profit locked")
        self.assertEqual(state["locked_pnl_usd"], 50.0)

    def test_short_side_comparisons_are_not_inverted(self) -> None:
        """A short trails *down*, so a naive > comparison gets it backwards."""
        from dashboard.data import build_stop_state

        state = build_stop_state("short", 2000.0, 1900.0, 2100.0, qty_open=0.5)

        self.assertTrue(state["trailed"])
        self.assertEqual(state["label"], "profit locked")
        self.assertEqual(state["locked_pnl_usd"], 50.0)


class TpLadderTests(unittest.TestCase):
    def test_targets_are_numbered_in_the_order_price_reaches_them(self) -> None:
        from dashboard.data import build_tp_progress

        rungs = build_tp_progress("short", 2000.0, [1800.0, 1900.0], legs=[])

        self.assertEqual([r["label"] for r in rungs], ["TP1", "TP2"])
        self.assertEqual(rungs[0]["price"], 1900.0)

    def test_stop_loss_legs_do_not_count_as_targets_hit(self) -> None:
        from dashboard.data import build_tp_progress

        legs = [
            {"reason": "stop_loss", "pnl_usd": -50.0, "qty": 0.5, "at": "2026-08-01"},
        ]
        rungs = build_tp_progress("long", 2000.0, [2100.0], legs=legs)

        self.assertFalse(rungs[0]["hit"])

    def test_paper_positions_fall_back_to_the_tps_hit_counter(self) -> None:
        from dashboard.data import build_tp_progress

        rungs = build_tp_progress("long", 2000.0, [2100.0, 2200.0], tps_hit=1)

        self.assertTrue(rungs[0]["hit"])
        self.assertFalse(rungs[1]["hit"])
        self.assertIsNone(rungs[0]["pnl_usd"])


if __name__ == "__main__":
    unittest.main()
