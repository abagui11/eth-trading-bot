"""Yield Generation tab payload — read-only proxy to the yield_gen_bot app.

Pulls live positions + the recommended-allocation plan from the Next.js
dashboard (``YIELD_GEN_API_URL``), summarizes them for the hub tab, and
records a daily NAV snapshot so the tab can show P&L since go-live.
Fail-soft: any upstream problem returns ``enabled/available`` flags instead
of raising into the page render.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import requests

import config
import live_ledger

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 15


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


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n else None  # NaN check


def _eth_usd_on_date(iso_date: str) -> float | None:
    """CoinGecko daily ETH/USD. Fail-soft — used only to backfill go-live price."""
    try:
        d = datetime.strptime(iso_date[:10], "%Y-%m-%d")
    except ValueError:
        return None
    stamped = d.strftime("%d-%m-%Y")
    try:
        res = requests.get(
            "https://api.coingecko.com/api/v3/coins/ethereum/history",
            params={"date": stamped, "localization": "false"},
            timeout=10,
        )
        res.raise_for_status()
        px = (
            res.json()
            .get("market_data", {})
            .get("current_price", {})
            .get("usd")
        )
        n = float(px)
        return n if n > 0 else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("ETH history fetch failed for %s: %s", iso_date, exc)
        return None


def derive_yield_metrics(
    *,
    nav_usd: float,
    projected_usd: float | None,
    projected_apy: float | None,
    nav_eth_now: float | None,
    eth_price_now: float | None,
    go_live_date: str | None,
    nav_start_usd: float | None,
    eth_price_start: float | None,
    today: date | None = None,
) -> dict[str, Any]:
    """Carry run-rate + ETH-NAV earnings from a go-live baseline."""
    today = today or datetime.now(timezone.utc).date()
    earned_usd = None
    days = None
    if projected_usd is not None and go_live_date:
        try:
            start = date.fromisoformat(go_live_date[:10])
            days = max((today - start).days, 0)
            earned_usd = round(projected_usd * days / 365.0, 2)
        except ValueError:
            days = None

    if nav_eth_now is None and eth_price_now and eth_price_now > 0:
        nav_eth_now = nav_usd / eth_price_now

    nav_eth_start = None
    pnl_eth = None
    if (
        nav_start_usd is not None
        and eth_price_start is not None
        and eth_price_start > 0
    ):
        nav_eth_start = nav_start_usd / eth_price_start
        if nav_eth_now is not None:
            pnl_eth = round(nav_eth_now - nav_eth_start, 6)

    return {
        "yield_projected_usd": (
            round(projected_usd, 2) if projected_usd is not None else None
        ),
        "yield_projected_apy": projected_apy,
        "yield_earned_usd": earned_usd,
        "days_since_golive": days,
        "nav_eth": round(nav_eth_now, 6) if nav_eth_now is not None else None,
        "nav_eth_start": (
            round(nav_eth_start, 6) if nav_eth_start is not None else None
        ),
        "pnl_eth": pnl_eth,
        "go_live_date": go_live_date,
        "nav_start_usd": (
            round(nav_start_usd, 2) if nav_start_usd is not None else None
        ),
        "eth_price_start_usd": eth_price_start,
        "eth_price_usd": eth_price_now,
    }


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

    topline = positions.get("topline") if isinstance(positions.get("topline"), dict) else {}
    eth_now = _f((topline or {}).get("ethPriceUsd"))
    projected_usd = _f((topline or {}).get("projectedUsd"))
    projected_apy = _f((topline or {}).get("projectedApy"))
    nav_eth_now = _f((topline or {}).get("navEthNow"))

    # ---- Daily NAV snapshot + P&L since go-live ----
    nav_series: list[dict[str, Any]] = []
    pnl_since_golive = None
    pnl_1d = None
    go_live_date = None
    nav_start_usd = None
    eth_price_start = None
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
                    eth_price_usd=eth_now,
                )
            nav_series = live_ledger.get_yield_nav_series(limit=90)
            if nav_series:
                first = nav_series[0]
                go_live_date = str(first.get("snapshot_date") or "") or None
                nav_start_usd = _f(first.get("nav_usd"))
                eth_price_start = _f(first.get("eth_price_usd"))
                if nav_start_usd:
                    pnl_since_golive = round(nav - nav_start_usd, 2)
                if len(nav_series) >= 2:
                    pnl_1d = round(nav - float(nav_series[-2]["nav_usd"]), 2)
                if eth_price_start is None and go_live_date:
                    eth_price_start = _eth_usd_on_date(go_live_date)
                    if eth_price_start:
                        live_ledger.backfill_yield_eth_price(
                            go_live_date, eth_price_start
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning("yield NAV snapshot failed: %s", exc)

    carry = derive_yield_metrics(
        nav_usd=nav,
        projected_usd=projected_usd,
        projected_apy=projected_apy,
        nav_eth_now=nav_eth_now,
        eth_price_now=eth_now,
        go_live_date=go_live_date,
        nav_start_usd=nav_start_usd,
        eth_price_start=eth_price_start,
    )

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
            {
                "date": r["snapshot_date"],
                "nav_usd": r["nav_usd"],
                "eth_price_usd": r.get("eth_price_usd"),
            }
            for r in nav_series
        ],
        "fetched_at": positions.get("fetchedAt"),
        "errors": (positions.get("errors") or [])[:3],
        **carry,
    }
