"""Eva close-chart: rationale-first copy, HQ-only, closed-only."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import bot_config
import config
import live_ledger
from tests.test_dashboard_ui import _sample_trade


THESIS = (
    "H4 supply rejected after Asia swept the prior high. "
    "M5 displacement through the order block confirmed the short."
)
MARKET_CTX = "Market context:\n• funding flipped persistent bull"


def _row(**over):
    base = {
        "id": 8,
        "cycle_id": "c1",
        "source": "hq",
        "status": "closed",
        "product_id": "ETH-USD",
        "instrument": "ETP-20DEC30-CDE",
        "side": "short",
        "qty": 0.4,
        "entry": 2500.0,
        "stop_loss": 2550.0,
        "initial_stop_loss": 2550.0,
        "take_profits_json": json.dumps([2460.0, 2420.0, 2380.0]),
        "exit_price": 2380.0,
        "pnl_usd": 48.0,
        "close_reason": "take_profit",
        "opened_at": "2026-08-31T08:29:00Z",
        "closed_at": "2026-08-31T09:24:00Z",
        "fill_type": "auto",
        "exit_fills_json": json.dumps(
            {
                "a": {
                    "qty": 0.13,
                    "price": 2460.0,
                    "pnl_usd": 5.2,
                    "reason": "take_profit",
                    "at": "2026-08-31T09:00:00Z",
                },
                "b": {
                    "qty": 0.13,
                    "price": 2420.0,
                    "pnl_usd": 10.4,
                    "reason": "take_profit",
                    "at": "2026-08-31T09:15:00Z",
                },
                "c": {
                    "qty": 0.14,
                    "price": 2380.0,
                    "pnl_usd": 16.8,
                    "reason": "take_profit",
                    "at": "2026-08-31T09:24:00Z",
                },
            }
        ),
    }
    base.update(over)
    return base


def _story(**over):
    base = {
        "action": "deriv_sell",
        "rationale": f"{THESIS}\n\n{MARKET_CTX}",
        "setup_tags": ["h4_ob", "m5_ob"],
        "stop_loss": 2550.0,
        "take_profits": [2460.0, 2420.0, 2380.0],
        "order_block": {"low": 2488.0, "high": 2512.0},
    }
    base.update(over)
    return base


def _bars(n: int = 50) -> list[dict]:
    start = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)
    px = 2500.0
    out: list[dict] = []
    for i in range(n):
        ts = start + timedelta(minutes=5 * i)
        drift = -i * 1.8
        o = px + drift
        h = o + 8
        low = o - 12
        c = o - 3
        out.append(
            {
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": 100.0 + i,
            }
        )
    return out


class CaseStudyRationaleTests(unittest.TestCase):
    """The original thesis is an input, not something the LLM is allowed to invent."""

    def test_market_context_is_stripped_so_only_the_thesis_is_fed_in(self) -> None:
        import case_study

        facts = case_study.build_facts(_row(), story=_story())
        self.assertIn("H4 supply rejected", facts["rationale"])
        self.assertNotIn("Market context", facts["rationale"])
        self.assertNotIn("funding flipped", facts["rationale"])

    def test_explicit_rationale_overrides_the_cycle_story(self) -> None:
        import case_study

        facts = case_study.build_facts(
            _row(),
            story=_story(),
            rationale="Operator short into the Jackson Hole spike.",
        )
        self.assertEqual(
            facts["rationale"], "Operator short into the Jackson Hole spike."
        )
        self.assertIn("Operator short", facts["rationale_excerpt"])

    def test_fallback_entry_copy_uses_the_thesis_excerpt(self) -> None:
        import case_study

        facts = case_study.build_facts(_row(), story=_story())
        copy = case_study.fallback_copy(facts)
        self.assertIn("H4 supply rejected", copy["bodies"]["1"])

    def test_llm_user_message_leads_with_the_rationale_then_facts(self) -> None:
        import case_study

        facts = case_study.build_facts(_row(), story=_story())
        facts["stop_touched"] = False
        facts["post_exit"] = {"low": 2370.0, "high": 2440.0, "moved_against_after_exit": True}

        response = MagicMock()
        block = MagicMock()
        block.type = "text"
        block.text = json.dumps(
            {
                "bodies": {"1": "Supply rejected, short filled into the sweep."},
                "misc_title": "PRICE REBOUNDED — FLAT ALREADY",
                "misc_body": "Low printed then reversed; book was already flat.",
            }
        )
        response.content = [block]
        client = MagicMock()
        client.messages.create.return_value = response

        with patch.object(bot_config, "USE_LLM_CASE_STUDY", True), patch(
            "anthropic.Anthropic", return_value=client
        ), patch("analyze.log_anthropic_usage"):
            parsed = case_study.llm_copy(facts)

        self.assertIsNotNone(parsed)
        user = client.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertTrue(user.startswith("ENTRY RATIONALE"))
        rationale_at = user.find(THESIS.split(".")[0])
        facts_at = user.find("FACTS (ledger + candles")
        self.assertGreater(rationale_at, 0)
        self.assertGreater(facts_at, rationale_at)
        self.assertNotIn("funding flipped", user)
        self.assertIn("Supply rejected", parsed["bodies"]["1"])

    def test_slots_are_entry_stop_partials_full_exit_then_misc(self) -> None:
        import case_study

        facts = case_study.build_facts(_row(), story=_story())
        facts["stop_touched"] = False
        facts["post_exit"] = {}
        slots = case_study.build_slots(facts, copy=case_study.fallback_copy(facts))
        kinds = [s.kind for s in slots]
        self.assertEqual(kinds[0], "entry")
        self.assertEqual(kinds[1], "stop")
        self.assertEqual(kinds[-1], "misc")
        self.assertEqual(kinds[-2], "exit")
        self.assertIn("SHORT ENTRY", slots[0].title)
        self.assertIn("FULL EXIT", slots[-2].title)
        self.assertTrue(any(s.kind == "tp" for s in slots))

    def test_runner_stopped_at_tp1_is_not_drawn_as_tp3(self) -> None:
        """Eva #8: TP1/TP2 paid, remainder died at the trailed stop — not TP3."""
        import case_study

        row = _row(
            side="long",
            entry=2411.5,
            stop_loss=2440.0,
            initial_stop_loss=2440.0,
            take_profits_json=json.dumps([2440.0, 2477.0, 2534.0]),
            exit_price=2456.125,
            close_reason="take_profit",
            exit_fills_json=json.dumps(
                {
                    "a": {
                        "qty": 0.1,
                        "price": 2468.5,
                        "pnl_usd": 5.7,
                        "reason": "take_profit",
                        "at": "2026-08-31T15:58:05Z",
                    },
                    "b": {
                        "qty": 0.1,
                        "price": 2477.0,
                        "pnl_usd": 6.55,
                        "reason": "take_profit",
                        "at": "2026-08-31T16:55:27Z",
                    },
                    "c": {
                        "qty": 0.2,
                        "price": 2439.5,
                        "pnl_usd": 5.6,
                        "reason": "take_profit",
                        "at": "2026-09-01T12:40:18Z",
                    },
                }
            ),
        )
        story = _story(
            action="deriv_buy",
            stop_loss=2385.0,
            take_profits=[2440.0, 2477.0, 2534.0],
        )
        facts = case_study.build_facts(row, story=story)
        facts["stop_touched"] = False
        facts["post_exit"] = {}
        slots = case_study.build_slots(facts, copy=case_study.fallback_copy(facts))
        titles = " ".join(s.title for s in slots)

        self.assertEqual(facts["close_reason"], "stop_loss")
        self.assertEqual(facts["initial_stop"], 2385.0)
        self.assertIn("TP1", titles)
        self.assertIn("TP2", titles)
        self.assertNotIn("TP3", titles)
        self.assertTrue(any(s.kind == "stopped" for s in slots))
        self.assertFalse(any(s.kind == "exit" for s in slots))


class CaseStudyGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self._charts = root / "charts"
        self._charts.mkdir()
        db = root / "ledger.db"
        db.write_bytes(b"")
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "CHARTS_DIR", self._charts),
            patch.object(config, "ROOT_DIR", root),
        ]
        for p in self._patches:
            p.start()
        live_ledger.init_db()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _open(self, **over) -> int:
        kwargs = dict(
            cycle_id="c1",
            source="hq",
            product_id="ETH-USD",
            instrument="ETP-20DEC30-CDE",
            side="short",
            qty=0.4,
            entry=2500.0,
            stop_loss=2550.0,
            take_profits_json=json.dumps([2460.0]),
            order_id="o1",
            stop_order_id="s1",
        )
        kwargs.update(over)
        return live_ledger.record_open(**kwargs)

    def test_mill_and_open_trades_are_skipped(self) -> None:
        import case_study

        mill = self._open(source="mill", cycle_id=None)
        live_ledger.record_close(mill, exit_price=2460.0, pnl_usd=4.0, close_reason="take_profit")
        self.assertIsNone(case_study.generate_for_trade(mill))

        open_id = self._open()
        self.assertIsNone(case_study.generate_for_trade(open_id))

    def test_generate_forwards_the_rationale_into_facts(self) -> None:
        import case_study

        tid = self._open()
        live_ledger.record_close(
            tid, exit_price=2460.0, pnl_usd=16.0, close_reason="take_profit"
        )
        seen: list[str | None] = []

        def _facts(row, *, story=None, rationale=None):
            seen.append(rationale)
            raise RuntimeError("stop after facts")

        with patch.object(case_study, "build_facts", side_effect=_facts), patch.object(
            case_study.logger, "exception"
        ):
            self.assertIsNone(
                case_study.generate_for_trade(
                    tid, rationale="H4 supply rejected after Asia swept."
                )
            )
        self.assertEqual(seen, ["H4 supply rejected after Asia swept."])

    def test_render_writes_a_png(self) -> None:
        import case_study

        facts = case_study.build_facts(_row(), story=_story())
        facts["stop_touched"] = False
        facts["post_exit"] = {
            "low": 2370.0,
            "low_at": "2026-08-31T09:25:00Z",
            "high": 2440.0,
            "high_at": "2026-08-31T10:17:00Z",
            "moved_against_after_exit": True,
        }
        slots = case_study.build_slots(facts, copy=case_study.fallback_copy(facts))
        out = self._charts / "case_study_hq_8.png"
        path = case_study.render_case_study(
            facts, slots, _bars(), tf_label="M5", out_path=out
        )
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 1000)

    def test_enrich_exposes_url_only_when_the_png_exists(self) -> None:
        from dashboard import data

        tid = self._open()
        live_ledger.record_close(
            tid, exit_price=2460.0, pnl_usd=16.0, close_reason="take_profit"
        )
        row = live_ledger.get_trade(tid)
        with patch.object(data, "_trade_story_from_cycle", return_value=_story()), patch.object(
            data, "trade_chart_urls", return_value={}
        ), patch.object(data, "get_live_spots", return_value={"spots": {}}):
            enriched = data.enrich_live_trades([row], closed=True)[0]
        self.assertIsNone(enriched.get("case_study_url"))

        png = self._charts / f"case_study_hq_{tid}.png"
        png.write_bytes(b"\x89PNG\r\n")
        live_ledger.set_case_study_path(tid, f"charts/{png.name}")
        row = live_ledger.get_trade(tid)
        with patch.object(data, "_trade_story_from_cycle", return_value=_story()), patch.object(
            data, "trade_chart_urls", return_value={}
        ), patch.object(data, "get_live_spots", return_value={"spots": {}}):
            enriched = data.enrich_live_trades([row], closed=True)[0]
        self.assertEqual(enriched["case_study_url"], f"/api/live-chart/{tid}")

    def test_api_serves_hq_closed_png_and_404s_otherwise(self) -> None:
        from dashboard.app import create_app
        from fastapi.testclient import TestClient

        tid = self._open()
        live_ledger.record_close(
            tid, exit_price=2460.0, pnl_usd=16.0, close_reason="take_profit"
        )
        png = self._charts / f"case_study_hq_{tid}.png"
        png.write_bytes(b"\x89PNG\r\nfake")
        live_ledger.set_case_study_path(tid, f"charts/{png.name}")

        mill = self._open(source="mill", cycle_id=None)
        live_ledger.record_close(
            mill, exit_price=2460.0, pnl_usd=1.0, close_reason="take_profit"
        )

        with patch(
            "dashboard.data.research.get_spot_prices",
            return_value={"ETH-USD": 2500.0, "BTC-USD": 60000.0},
        ), patch.object(config, "INVESTOR_ACCESS_TOKEN", None):
            client = TestClient(create_app())
            try:
                ok = client.get(f"/api/live-chart/{tid}")
                self.assertEqual(ok.status_code, 200)
                self.assertEqual(ok.headers["content-type"], "image/png")
                self.assertEqual(client.get(f"/api/live-chart/{mill}").status_code, 404)
                self.assertEqual(client.get("/api/live-chart/9999").status_code, 404)
            finally:
                client.close()


class CaseStudyMacroTests(unittest.TestCase):
    def test_closed_card_renders_the_case_study_figure(self) -> None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        from dashboard.formatting import (
            format_trade_date,
            format_trade_time,
            tag_tooltip,
            trade_title,
        )

        root = Path(__file__).resolve().parents[1] / "dashboard" / "templates"
        env = Environment(
            loader=FileSystemLoader(str(root)), autoescape=select_autoescape(["html"])
        )
        env.filters["trade_time"] = format_trade_time
        env.filters["trade_date"] = format_trade_date
        env.filters["tag_tip"] = tag_tooltip
        env.globals["trade_title"] = trade_title
        tmpl = env.from_string(
            "{% from '_macros.html' import trade_card %}"
            "{{ trade_card(t, 'closed') }}"
        )
        html = tmpl.render(
            t=_sample_trade(
                case_study_url="/api/live-chart/8",
                side="short",
                product_label="ETH",
            )
        )
        self.assertIn("trade-case-study", html)
        self.assertIn("/api/live-chart/8", html)
        self.assertIn("Eva trade case study", html)

    def test_open_card_does_not_show_the_case_study(self) -> None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        from dashboard.formatting import (
            format_trade_date,
            format_trade_time,
            tag_tooltip,
            trade_title,
        )

        root = Path(__file__).resolve().parents[1] / "dashboard" / "templates"
        env = Environment(
            loader=FileSystemLoader(str(root)), autoescape=select_autoescape(["html"])
        )
        env.filters["trade_time"] = format_trade_time
        env.filters["trade_date"] = format_trade_date
        env.filters["tag_tip"] = tag_tooltip
        env.globals["trade_title"] = trade_title
        tmpl = env.from_string(
            "{% from '_macros.html' import trade_card %}"
            "{{ trade_card(t, 'open') }}"
        )
        html = tmpl.render(
            t=_sample_trade(
                case_study_url="/api/live-chart/8",
                side="short",
                product_label="ETH",
            )
        )
        self.assertNotIn("trade-case-study", html)


class CandleWindowTests(unittest.TestCase):
    def test_fetch_bars_does_not_ask_coinbase_for_the_future(self) -> None:
        """A day-long trade plus H1 pad-after used to request candles past now."""
        import case_study

        facts = {
            "opened_at": "2026-08-31T02:00:56Z",
            "closed_at": "2026-09-01T12:40:18Z",
            "product_id": "ETH-USD",
        }
        captured: dict[str, int | str] = {}

        def fake_range(gran, start, end, product_id="ETH-USD"):
            captured["gran"] = gran
            captured["start"] = start
            captured["end"] = end
            return [{"ts": "2026-08-31T02:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]

        now = datetime(2026, 9, 1, 15, 20, tzinfo=timezone.utc).timestamp()
        with (
            patch.object(case_study.research, "fetch_coinbase_candles_range", fake_range),
            patch.object(case_study.time, "time", return_value=now),
        ):
            case_study.fetch_bars(facts)

        self.assertLessEqual(int(captured["end"]), int(now))
        self.assertLess(int(captured["start"]), int(captured["end"]))


if __name__ == "__main__":
    unittest.main()
