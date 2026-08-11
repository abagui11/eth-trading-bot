"""Public brain payload for the Intelligence Hub dashboard (no service token).

Reads local intel + macro stores so the HTML dashboard can show what the
Republic Technologies brain is ingesting without exposing gated HQ ideas.
"""

from __future__ import annotations

from typing import Any

import bot_config
from dashboard import data
from dashboard.charts import stance_chart_path
from intelligence import store as intel_store
from macro.context import macro_payload_for_dashboard


_STANCE_PRODUCTS: tuple[str, ...] = ("BTC-USD", "ETH-USD")
_STANCE_TIMEFRAMES: tuple[str, ...] = ("H4", "H1", "M15")


def _structure_board(stances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Marked H4/H1/M15 charts per product, paired with the posture they back.

    LLM stances land after the programmatic ones in the same cycle, so the last
    entry per (product, timeframe) is the one currently on display.
    """
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in stances:
        key = (str(row.get("product_id")), str(row.get("timeframe")))
        latest[key] = row

    board: list[dict[str, Any]] = []
    for product_id in _STANCE_PRODUCTS:
        charts: list[dict[str, Any]] = []
        for tf in _STANCE_TIMEFRAMES:
            if stance_chart_path(product_id, tf) is None:
                continue
            row = latest.get((product_id, tf), {})
            charts.append(
                {
                    "timeframe": tf,
                    "stance": row.get("stance"),
                    "confidence": row.get("confidence"),
                    "rationale": row.get("rationale"),
                    # Files are overwritten in place each cycle — bust the cache.
                    "url": f"/api/brain/structure/{product_id}/{tf}?v={row.get('created_at') or ''}",
                }
            )
        if charts:
            board.append(
                {
                    "product_id": product_id,
                    "product_label": bot_config.product_label(product_id),
                    "charts": charts,
                }
            )
    return board


def get_brain_payload() -> dict[str, Any]:
    spots = data.get_live_spots()
    stances = [
        {
            "cycle_ts": s.get("cycle_ts"),
            "product_id": s.get("product_id"),
            "timeframe": s.get("timeframe"),
            "stance": s.get("stance"),
            "confidence": s.get("confidence"),
            "rationale": s.get("rationale"),
            "source": s.get("source"),
            "created_at": s.get("created_at"),
        }
        for s in intel_store.latest_stances()
    ]
    thesis_row = intel_store.latest_long_thesis()
    thesis = None
    if thesis_row:
        body = thesis_row.get("thesis") or {}
        thesis = {
            "as_of_date": thesis_row.get("as_of_date"),
            "cycle_phase": thesis_row.get("cycle_phase"),
            "bias": body.get("bias"),
            "btc_thesis": body.get("btc_thesis"),
            "eth_conduit": body.get("eth_conduit"),
            "gold_note": body.get("gold_note"),
            "gold_ratios": body.get("gold_ratios"),
            "risks": body.get("risks"),
            "summary": body.get("summary"),
            "cycle_position": body.get("cycle_position"),
            "cycle_segments": body.get("cycle_segments") or [],
            "chart_url": "/api/brain/cycle-chart"
            if thesis_row.get("chart_path")
            else None,
            "figure_url": "/api/brain/cycle-figure"
            if body.get("cycle_figure_path")
            else None,
        }
    medium = intel_store.latest_medium_summary() or {}
    funding = {}
    for pid in bot_config.FUNDING_PRODUCTS:
        regime = intel_store.latest_funding_regime(pid)
        funding[pid] = {
            "regime": (regime or {}).get("regime"),
            "streak_periods": (regime or {}).get("streak_periods"),
            "as_of_ts": (regime or {}).get("as_of_ts"),
        }
    structure_charts = _structure_board(stances)
    zmoves = intel_store.recent_zmove_events(limit=12)
    macro = macro_payload_for_dashboard()
    # Rank news by absolute conviction then severity.
    for key in ("active", "recent"):
        items = list(macro.get(key) or [])
        items.sort(
            key=lambda e: (
                -(int(e.get("bias_pct") or 0)),
                -(int(e.get("severity") or 0)),
            )
        )
        macro[key] = items

    return {
        "spots": spots.get("spots") or {},
        "spot_as_of": spots.get("as_of"),
        "stances": stances,
        "cycle_ts": stances[0]["cycle_ts"] if stances else None,
        "medium": {
            "summary": medium.get("summary"),
            "btc_eth_note": medium.get("btc_eth_note"),
            "funding_note": medium.get("funding_note"),
        },
        "long_thesis": thesis,
        "funding": funding,
        "structure_charts": structure_charts,
        "zmoves": zmoves,
        "macro": macro,
        "intelligence_enabled": bot_config.INTELLIGENCE_ENABLED,
    }
