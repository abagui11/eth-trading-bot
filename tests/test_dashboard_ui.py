"""Basic UI structure / CSS smoke checks for the trade journal."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


def _sample_trade(**overrides):
    base = {
        "side": "long",
        "action": "deriv_buy",
        "entry": 3200.0,
        "avg_entry": 3200.0,
        "exit": 3300.0,
        "spot": 3250.0,
        "pnl_usd": 50.0,
        "pnl_pct": 3.1,
        "is_winner": True,
        "opened_at": "2026-07-14T16:00:00Z",
        "closed_at": "2026-07-14T18:00:00Z",
        "close_reason": "take_profit",
        "open_cycle_id": "20260714T160000Z",
        "close_cycle_id": "20260714T180000Z",
        "stop_loss": 3100.0,
        "take_profits": [3300.0],
        "risk_reward": 2.0,
        "eth_qty": 0.5,
        "qty": 0.5,
        "size_usd": 1600.0,
        "product_label": "ETH",
        "order_block": None,
        "setup_tags": ["h4_ob"],
        "rationale": "Test rationale for structure.",
        "structure_chart_url": "/api/chart/x?kind=structure&tf=H4",
        "execution_chart_url": "/api/chart/x?kind=entry&tf=M5",
        "thumb_chart_url": "/api/chart/x?kind=entry&tf=M5",
        "dist_to_sl_pct": 2.0,
        "dist_to_tp_pct": 1.0,
        "unrealized_pnl_usd": 25.0,
    }
    base.update(overrides)
    return base


class DashboardUiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmpdir.name)
        self._db = root / "ledger.db"
        self._charts = root / "charts"
        self._charts.mkdir()
        self._db.write_bytes(b"")

        self._patches = [
            patch.object(config, "LEDGER_DB", self._db),
            patch.object(config, "CHARTS_DIR", self._charts),
            patch.object(config, "ROOT_DIR", root),
            patch(
                "dashboard.data.research.get_spot_prices",
                return_value={"ETH-USD": 2000.0, "BTC-USD": 60000.0},
            ),
            patch(
                "dashboard.data.get_status_payload",
                return_value={
                    "spot": 2000.0,
                    "headline": "Flat",
                    "alerts": [],
                    "watching": [],
                    "phase": "idle",
                    "ts": None,
                    "cycle_id": None,
                    "chart_read_score": None,
                    "score_badge": "none",
                    "h4_chart_url": "/api/chart/latest",
                    "h4_charts": [
                        {
                            "product_id": "ETH-USD",
                            "product_label": "ETH",
                            "cycle_id": "test_ETH",
                            "url": "/api/chart/test_ETH",
                        },
                        {
                            "product_id": "BTC-USD",
                            "product_label": "BTC",
                            "cycle_id": "test_BTC",
                            "url": "/api/chart/test_BTC",
                        },
                    ],
                },
            ),
            patch(
                "dashboard.data.get_performance_payload",
                return_value={
                    "equity_usd": 5000.0,
                    "total_pnl_usd": 0.0,
                    "total_pnl_pct": 0.0,
                    "realized_pnl_usd": 0.0,
                    "avg_pnl_usd": None,
                    "profit_factor": None,
                    "win_rate_pct": 0.0,
                    "starting_usd": 5000.0,
                    "closed_trade_count": 1,
                    "open_count": 1,
                    "open_by_product": {},
                    "chart_read": {"avg_score_30d": None, "issue_rate_pct": 0},
                    "epoch": {
                        "epoch_label": "5k_usd",
                        "epoch_started_at": None,
                    },
                },
            ),
            patch(
                "dashboard.data.get_open_positions_payload",
                return_value=[_sample_trade(exit=None, close_reason=None, status="open")],
            ),
            patch(
                "dashboard.data.get_closed_trades_payload",
                return_value=[_sample_trade()],
            ),
            patch("dashboard.data.get_archived_trades_payload", return_value=[]),
            patch("dashboard.data.get_cycles", return_value=[]),
            patch(
                "dashboard.data.get_macro_payload",
                return_value={
                    "enabled": True,
                    "posture": {
                        "eth_bias": "neutral",
                        "max_severity": 0,
                        "gate_long": False,
                        "gate_short": False,
                    },
                    "monitored_sources": ["test"],
                    "active": [
                        {
                            "severity": 4,
                            "eth_bias": "bearish",
                            "title": "Test macro",
                            "url": "https://www.cnbc.com/2026/08/10/test.html",
                            "published_at": "2026-08-10T10:45:43Z",
                            "ingested_at": "2026-08-10T10:50:00Z",
                            "eth_impact_summary": "Impact",
                        }
                    ],
                    "recent": [
                        {
                            "severity": 2,
                            "eth_bias": "neutral",
                            "title": "Quiet wire story",
                            "url": None,
                            "source": "Reuters: Business News",
                            "keyword_score": 3,
                            "status": "ignored",
                            "ingested_at": "Mon, 10 Aug 2026 09:00:00 +0000",
                        }
                    ],
                },
            ),
        ]
        for p in self._patches:
            p.start()

        from dashboard.app import create_app
        from fastapi.testclient import TestClient

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        for p in reversed(self._patches):
            p.stop()
        self._tmpdir.cleanup()

    def test_live_book_uses_the_same_cards_as_the_paper_journal(self) -> None:
        """Eva's live rows were a bare table: no thesis, no chart, no mark."""
        live_row = {
            "id": 1,
            "cycle_id": "20260831T020000Z",
            "source": "hq",
            "product_id": "ETH-USD",
            "instrument": "ETH-27JUN25-CDE",
            "side": "long",
            "qty": 0.4,
            "entry": 1900.0,
            "stop_loss": 1850.0,
            "fill_type": "auto",
            "opened_at": "2026-08-31T02:00:56Z",
        }
        story = {
            "action": "deriv_buy",
            "rationale": "Live thesis should be readable from the card.",
            "setup_tags": ["h4_ob"],
            "stop_loss": 1850.0,
            "take_profits": [2050.0],
            "risk_reward": 2.4,
            "chart_path": None,
            "marked_chart_paths": {"H4": "x.png"},
        }
        with patch("live_ledger.get_open_trades", return_value=[live_row]), patch(
            "live_ledger.get_closed_trades", return_value=[]
        ), patch("dashboard.data._trade_story_from_cycle", return_value=story), patch(
            "dashboard.data.trade_chart_urls",
            return_value={
                "structure_chart_url": "/api/chart/live?kind=structure&tf=H4",
                "execution_chart_url": None,
                "thumb_chart_url": "/api/chart/live?kind=structure&tf=H4",
            },
        ):
            html = self.client.get("/").text

        live_card = html.split('id="live-book-card"', 1)[1].split("</section>", 1)[0]
        # Same accordion + thumbnail treatment as the paper journal.
        self.assertIn('class="trade-card trade-live"', live_card)
        self.assertIn("Live thesis should be readable from the card.", live_card)
        self.assertIn("/api/chart/live?kind=structure&amp;tf=H4", live_card)
        self.assertIn("trade-thumb", live_card)
        # Spot 2000 vs entry 1900 on 0.4 qty — the mark the table never showed.
        self.assertIn("mark $2000.00", live_card)
        self.assertIn("+40.00", live_card)
        self.assertIn("auto", live_card)

    def test_trade_cards_use_button_accordion_collapsed(self) -> None:
        html = self.client.get("/").text
        # No native <details> — avoids double disclosure arrows.
        self.assertNotIn("<details", html)
        self.assertNotIn("<summary", html)
        self.assertIn('class="trade-card trade-live"', html)
        self.assertIn('class="trade-title"', html)
        self.assertIn("Jul 14 [long]", html)
        self.assertIn("4:00 PM", html)
        self.assertNotIn("2026-07-14T16:00", html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('class="trade-body" hidden', html)
        self.assertIn("initTradeCards", html)
        self.assertIn("initChartLightbox", html)
        self.assertIn('id="chart-lightbox"', html)
        self.assertIn("zoomable", html)
        self.assertIn('title="Long position', html)
        # Ignore the client-side "load more" card template embedded in <script>.
        rendered_html = html.split("<script", 1)[0]
        n_buttons = len(
            re.findall(
                r'<button type="button" class="trade-summary"',
                rendered_html,
            )
        )
        n_bodies = len(re.findall(r'class="trade-body" hidden', rendered_html))
        self.assertEqual(n_buttons, 2)
        self.assertEqual(n_bodies, 2)

    def test_css_image_caps_and_news_feed(self) -> None:
        css = self.client.get("/static/style.css").text
        self.assertIn(".trade-body[hidden]", css)
        self.assertIn(".trade-summary-main", css)
        self.assertIn("max-height: 280px", css)
        self.assertIn("max-width: 100%", css)
        # The macro monitor shares the news desk styles; the old narrow square
        # box and its list rules are gone.
        self.assertIn(".news-feed", css)
        self.assertIn(".news-section", css)
        for dead in (".macro-scroll", ".macro-item", ".macro-list", ".macro-impact"):
            self.assertNotIn(dead, css)
        self.assertIn(".trade-thumb-wrap", css)
        self.assertIn(".trade-chart .chart-img", css)
        self.assertIn(".trade-case-study", css)
        self.assertIn("height: 200px", css)
        self.assertIn("gap: 20px", css)
        self.assertIn("display: flex", css)
        self.assertIn(".chart-lightbox", css)
        self.assertIn("cursor: zoom-in", css)
        self.assertIn("tr.mill-clip-row", css)
        self.assertIn(".mill-clip-detail", css)
        self.assertNotIn("<details", self.client.get("/").text)
        html = self.client.get("/").text
        self.assertNotIn("macro-scroll", html)
        self.assertIn('<div class="news-feed" id="macro-feed">', html)
        self.assertIn("h4-charts", html)
        self.assertIn("ETH-USD · H4", html)
        self.assertIn("BTC-USD · H4", html)

    def test_macro_monitor_renders_as_news_desk(self) -> None:
        html = self.client.get("/").text
        macro_card = html.split('id="macro-card"', 1)[1].split("</section>", 1)[0]
        # Same component as the Brain tab's news desk.
        self.assertIn('<div class="news-feed" id="macro-feed">', macro_card)
        self.assertIn('<ul class="news-list" id="macro-active">', macro_card)
        self.assertIn('<ul class="news-list" id="macro-recent">', macro_card)
        self.assertIn('class="news-item', macro_card)
        self.assertIn('class="news-meta"', macro_card)
        self.assertNotIn("macro-item", macro_card)
        # Datelines — absent from the old markup.
        self.assertIn('<time class="news-when" datetime="2026-08-10T10:45:43+00:00">', macro_card)
        self.assertIn("Aug 10 · 10:45 UTC", macro_card)
        self.assertIn("Aug 10 · 09:00 UTC", macro_card)
        self.assertIn('class="news-source">cnbc.com<', macro_card)
        self.assertIn('class="news-source">Reuters<', macro_card)
        self.assertIn('class="news-age"', macro_card)
        # Eva Trades tab still has the macro monitor.
        self.assertIn("Active (injected into agent)", macro_card)
        self.assertIn("Recent ingested", macro_card)
        self.assertIn("kw 3", macro_card)
        self.assertIn('class="macro-status">ignored<', macro_card)
        self.assertIn('class="news-item macro-ignored"', macro_card)
        self.assertIn('class="news-blurb">Impact<', macro_card)
        self.assertIn('id="macro-posture"', macro_card)
        self.assertIn("Sources:", macro_card)

    def test_brain_news_desk_unchanged(self) -> None:
        html = self.client.get("/").text
        brain_card = html.split('id="brain-news"', 1)[1].split("</section>", 1)[0]
        self.assertIn('<div class="news-feed" id="brain-news-feed">', brain_card)
        self.assertIn("News desk", brain_card)

    def test_hub_has_four_product_tabs(self) -> None:
        html = self.client.get("/").text
        self.assertIn('data-tab="brain"', html)
        self.assertIn('data-tab="trading"', html)
        self.assertIn('data-tab="yield"', html)
        self.assertIn('data-tab="mill"', html)
        self.assertIn('id="tab-mill"', html)
        self.assertIn("Trade mill · live clip (internal)", html)
        self.assertIn("Eva Trades", html)
        self.assertIn("Eva live book", html)
        self.assertIn("ICT decisions Eva makes", html)
        self.assertIn("Eva paper book · v2", html)
        self.assertIn("currently live", html)
        self.assertIn("id=\"paper-journal-toggle\"", html)
        self.assertIn("id=\"paper-journal-body\" hidden", html)
        self.assertIn("Show paper journal", html)
        self.assertNotIn("Paper journal · v2", html)
        mill_card = html.split('id="mill-clip-card"', 1)[1].split("</section>", 1)[0]
        self.assertIn("nano ETH", mill_card)
        self.assertIn("/feed", mill_card)
        self.assertIn("id=\"mill-live-open\"", mill_card)
        self.assertIn("id=\"mill-live-open-table\"", mill_card)
        self.assertIn("id=\"mill-live-closed-table\"", mill_card)
        self.assertIn("<th>Opened</th>", mill_card)
        self.assertIn("<th>Closed</th>", mill_card)
        self.assertNotIn("trade-card", mill_card)
        self.assertIn("millClipRowHtml", html)
        self.assertIn("initMillClipRows", html)

    def test_mill_clips_render_as_expandable_table_rows(self) -> None:
        mill_row = {
            "id": 13,
            "source": "mill",
            "product_id": "BTC-USD",
            "instrument": "BIP-20DEC30-CDE",
            "side": "short",
            "qty": 0.01,
            "entry": 78305.0,
            "stop_loss": 78629.22,
            "take_profits_json": "[77254.43]",
            "fill_type": "auto",
            "opened_at": "2026-09-01T14:10:21Z",
        }

        def _open(source=None, **_kwargs):
            return [mill_row] if source == "mill" else []

        with patch("live_ledger.get_open_trades", side_effect=_open), patch(
            "live_ledger.get_closed_trades", return_value=[]
        ):
            html = self.client.get("/").text
        mill_card = html.split('id="mill-clip-card"', 1)[1].split("</section>", 1)[0]
        self.assertIn('class="mill-clip-row"', mill_card)
        self.assertIn("class=\"mill-clip-detail\"", mill_card)
        self.assertIn("BIP-20DEC30-CDE", mill_card)
        self.assertIn("R:R", mill_card)
        self.assertNotIn('class="trade-card', mill_card)
        self.assertNotIn("trade-summary", mill_card)

    def test_archived_journal_is_collapsed_with_v1_metrics(self) -> None:
        summary = {
            "available": True,
            "epoch_label": "legacy_1k",
            "starting_usd": 1000.0,
            "ended_at": "2026-07-16T00:00:00Z",
            "closed_trade_count": 1,
            "win_rate_pct": 100.0,
            "realized_pnl_usd": 50.0,
            "realized_pnl_pct": 5.0,
            "avg_pnl_usd": 50.0,
            "avg_win_usd": 50.0,
            "avg_loss_usd": None,
            "profit_factor": None,
        }
        with (
            patch(
                "dashboard.data.get_archived_trades_payload",
                return_value=[_sample_trade()],
            ),
            patch(
                "dashboard.data.get_archived_performance_payload",
                return_value=summary,
            ),
        ):
            html = self.client.get("/").text
        self.assertIn("Archived trades · v1", html)
        self.assertIn("Paper book v1", html)
        self.assertIn("id=\"archived-journal-toggle\"", html)
        self.assertIn("id=\"archived-journal-body\" hidden", html)
        self.assertIn("Show v1 journal", html)
        self.assertIn("legacy_1k", html)


if __name__ == "__main__":
    unittest.main()
