"""Yield Generation tab payload — read-only proxy to the yield_gen_bot app.

Pulls live positions + the recommended-allocation plan from the Next.js
dashboard (``YIELD_GEN_API_URL``), summarizes them for the hub tab, and
records a daily NAV snapshot so the tab can show P&L since go-live.
Fail-soft: any upstream problem returns ``enabled/available`` flags instead
of raising into the page render.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

import config
import live_ledger

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 8


def _fetch_json(path: str) -> dict[str, Any] | None:
    base = (config.YIELD_GEN_API_URL or "").rstrip("/")
    if not base:
        return None
    try:
        res = requests.get(f"{base}{path}", timeout=_TIMEOUT_SEC)
        res.raise_for_status()
        return res.json()
    except Exception as exc:  # noqa: BLE001 — fail-soft into the tab
        logger.warning("yield_gen fetch %s failed: %s", path, exc)
        return None


def get_yield_payload() -> dict[str, Any]:
    if not config.YIELD_GEN_API_URL:
        return {
            "enabled": False,
            "error": "YIELD_GEN_API_URL unset — point it at the yield_gen_bot app",
        }

    positions = _fetch_json("/api/positions")
    plan_res = _fetch_json("/api/plan")

    if positions is None:
        return {
            "enabled": True,
            "available": False,
            "error": "yield_gen_bot unreachable",
            "dashboard_url": config.YIELD_GEN_DASHBOARD_URL,
        }

    # ---- Aggregate wallet state ----
    collateral = 0.0
    debt = 0.0
    worst_hf: float | None = None
    for pos in positions.get("aave") or []:
        collateral += float(pos.get("totalCollateralUsd") or 0)
        debt += float(pos.get("totalDebtUsd") or 0)
        hf = pos.get("healthFactor")
        if hf is not None:
            hf_f = float(hf)
            worst_hf = hf_f if worst_hf is None else min(worst_hf, hf_f)

    pt_total = 0.0
    pendle: list[dict[str, Any]] = []
    for p in positions.get("pendle") or []:
        val = float(p.get("valuationUsd") or 0)
        pt_total += val
        pendle.append(
            {
                "name": p.get("name"),
                "valuation_usd": round(val, 2),
                "maturity": p.get("maturity"),
            }
        )

    # NAV: equity in the Aave account plus PT holdings. PT positions bought
    # with borrowed stables are the asset side of that same debt, so
    # collateral − debt + PTs is the sleeve's net equity.
    nav = collateral - debt + pt_total

    monitors = [
        {
            "monitor": m.get("monitor"),
            "state": m.get("state"),
            "label": m.get("label"),
            "recommendation": m.get("recommendation"),
        }
        for m in positions.get("monitors") or []
    ]
    alerting = [m for m in monitors if m.get("state") not in (None, "OK")]

    # ---- Daily NAV snapshot + P&L since go-live ----
    nav_series: list[dict[str, Any]] = []
    pnl_since_golive = None
    pnl_1d = None
    if positions.get("enabled"):
        try:
            live_ledger.init_db()
            # Don't record dust: an unfunded wallet (or a partially failed
            # sync reading everything as 0) must never become the go-live
            # baseline or overwrite a real day's mark.
            if nav >= 50:
                live_ledger.record_yield_nav(
                    nav_usd=nav,
                    collateral_usd=collateral,
                    debt_usd=debt,
                    pt_usd=pt_total,
                    health_factor=worst_hf,
                )
            nav_series = live_ledger.get_yield_nav_series(limit=90)
            if nav_series:
                first = float(nav_series[0]["nav_usd"])
                if first:
                    pnl_since_golive = round(nav - first, 2)
                if len(nav_series) >= 2:
                    pnl_1d = round(nav - float(nav_series[-2]["nav_usd"]), 2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("yield NAV snapshot failed: %s", exc)

    # ---- Plan summary ----
    plan_summary: dict[str, Any] | None = None
    if plan_res and isinstance(plan_res.get("plan"), dict):
        plan = plan_res["plan"]
        plan_summary = {
            "status": plan.get("status"),
            "blocked_reason": plan.get("blockedReason"),
            "target_beta": plan.get("targetNetBeta"),
            "planned_beta": plan.get("plannedNetBeta"),
            "target_hf": plan.get("targetHf"),
            "actions": [a.get("description") for a in plan.get("actions") or []],
            "warnings": plan.get("warnings") or [],
        }

    return {
        "enabled": True,
        "available": True,
        "live_read": bool(positions.get("enabled")),
        "dashboard_url": config.YIELD_GEN_DASHBOARD_URL,
        "nav_usd": round(nav, 2),
        "collateral_usd": round(collateral, 2),
        "debt_usd": round(debt, 2),
        "pt_usd": round(pt_total, 2),
        "health_factor": worst_hf,
        "pnl_since_golive_usd": pnl_since_golive,
        "pnl_1d_usd": pnl_1d,
        "pendle": pendle,
        "monitors": monitors,
        "alerting": alerting,
        "plan": plan_summary,
        "nav_series": [
            {"date": r["snapshot_date"], "nav_usd": r["nav_usd"]}
            for r in nav_series
        ],
        "fetched_at": positions.get("fetchedAt"),
        "errors": (positions.get("errors") or [])[:3],
    }
