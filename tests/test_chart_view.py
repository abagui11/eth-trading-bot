"""Tests for chart_view — latest chart resolution and watch summary."""

from __future__ import annotations

from pathlib import Path

import audit
import chart_view
import config
import ledger
from models import Suggestion
from patterns.market_context import MarketContext
from patterns.order_block import OrderBlock
from patterns.range_24h import Range24h


def _init_dbs(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    charts = tmp_path / "charts"
    charts.mkdir()
    monkeypatch.setattr(config, "LEDGER_DB", db)
    monkeypatch.setattr(config, "CHARTS_DIR", charts)
    ledger.init_db()
    audit.init_db()


def _fake_market_context() -> MarketContext:
    return MarketContext(
        range_24h=Range24h(
            high=2550.0,
            low=2450.0,
            mid=2500.0,
            width_pct=4.0,
            is_ranging=True,
            bars_in_range=18,
            start_ts="2026-07-01T00:00:00Z",
            end_ts="2026-07-02T12:00:00Z",
        ),
        is_ranging=True,
        range_break=None,
        spot=2500.0,
        zone_snapshot=None,
        setup_state=None,
        alerts=["price near weekly open"],
        order_blocks=[
            OrderBlock(
                direction="bearish",
                low=2490.0,
                high=2510.0,
                start_ts="t0",
                end_ts="t1",
                displacement_ts="t2",
            )
        ],
        setup_tags=["range_24h_ranging"],
        summary_text="test summary",
    )


def test_get_latest_chart_view_uses_output_chart(tmp_path, monkeypatch):
    _init_dbs(tmp_path, monkeypatch)
    chart = config.CHARTS_DIR / "20260702T120000Z_H4_notrade.png"
    chart.write_bytes(b"png")

    ledger.append(
        Suggestion.no_trade(rationale="No setup"),
        cycle_id="20260702T120000Z",
        price_at_suggestion=2500.0,
        chart_path=str(chart),
        setup_tags="range_24h_ranging",
    )
    audit.save_snapshot(
        "20260702T120000Z",
        _fake_market_context(),
        Suggestion.no_trade(rationale="No setup"),
        {"H4": str(chart)},
    )

    view = chart_view.get_latest_chart_view()
    assert view is not None
    assert view.cycle_id == "20260702T120000Z"
    assert view.chart_paths == [str(chart)]
    assert "24h range" in view.watch_summary
    assert "M5 bearish OB" in view.watch_summary
    assert "NO_TRADE" in view.caption.upper()


def test_get_latest_chart_view_falls_back_to_marked_chart(tmp_path, monkeypatch):
    _init_dbs(tmp_path, monkeypatch)
    marked = config.CHARTS_DIR / "20260702T130000Z_H4_marked.png"
    marked.write_bytes(b"png")

    ledger.append(
        Suggestion.no_trade(rationale="Missing output chart"),
        cycle_id="20260702T130000Z",
        price_at_suggestion=2400.0,
        chart_path="charts/missing.png",
    )
    audit.save_snapshot(
        "20260702T130000Z",
        _fake_market_context(),
        Suggestion.no_trade(rationale="Missing output chart"),
        {"H4": str(marked)},
    )

    view = chart_view.get_latest_chart_view()
    assert view is not None
    assert view.chart_paths == [str(marked)]
