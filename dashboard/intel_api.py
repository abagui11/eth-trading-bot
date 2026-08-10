"""Versioned intelligence API (/api/v1) for service consumers.

Consumers: yield_gen_bot (HTF posture panel) and the trade_ideas mill.

Every route requires a Bearer service token (SERVICE_API_TOKENS). The dashboard
host is publicly reachable, so this API fails closed: with no tokens configured
it returns 503 rather than serving reads anonymously.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse

import access
import bot_config
import config
import ledger
from intelligence import store as intel_store
from macro import store as macro_store


def _require_service_auth(authorization: str | None) -> None:
    tokens = list(config.SERVICE_API_TOKENS)
    if config.MACRO_WEBHOOK_SECRET:
        tokens.append(config.MACRO_WEBHOOK_SECRET)
    if not tokens:
        raise HTTPException(
            status_code=503,
            detail="No service tokens configured (SERVICE_API_TOKENS)",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    presented = authorization[len("Bearer ") :].strip()
    if presented not in tokens:
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _service_auth(authorization: str | None = Header(default=None)) -> None:
    _require_service_auth(authorization)


router = APIRouter(prefix="/api/v1", dependencies=[Depends(_service_auth)])


def _stance_entry(row: dict) -> dict:
    return {
        "cycle_ts": row.get("cycle_ts"),
        "product_id": row.get("product_id"),
        "timeframe": row.get("timeframe"),
        "stance": row.get("stance"),
        "confidence": row.get("confidence"),
        "rationale": row.get("rationale"),
        "source": row.get("source"),
        "created_at": row.get("created_at"),
    }


def _funding_payload(product_id: str) -> dict:
    regime = intel_store.latest_funding_regime(product_id)
    series = intel_store.funding_series(product_id, limit=90)
    return {
        "product_id": product_id,
        "regime": regime,
        "series": series,
    }


@router.get("/intelligence/latest")
async def intelligence_latest() -> dict:
    stances = [_stance_entry(s) for s in intel_store.latest_stances()]
    medium = intel_store.latest_medium_summary()
    thesis = intel_store.latest_long_thesis()
    funding = {
        pid: intel_store.latest_funding_regime(pid)
        for pid in bot_config.FUNDING_PRODUCTS
    }
    return {
        "enabled": bot_config.INTELLIGENCE_ENABLED,
        "cycle_ts": stances[0]["cycle_ts"] if stances else None,
        "stances": stances,
        "medium": {
            "summary": (medium or {}).get("summary"),
            "btc_eth_note": (medium or {}).get("btc_eth_note"),
            "funding_note": (medium or {}).get("funding_note"),
            "cycle_ts": (medium or {}).get("cycle_ts"),
        },
        "funding_regimes": funding,
        "long_thesis": {
            "as_of_date": (thesis or {}).get("as_of_date"),
            "cycle_phase": (thesis or {}).get("cycle_phase"),
            "thesis": (thesis or {}).get("thesis"),
            "chart_url": "/api/v1/charts/cycle" if (thesis or {}).get("chart_path") else None,
        }
        if thesis
        else None,
    }


@router.get("/intelligence/history")
async def intelligence_history(limit: int = 100, offset: int = 0) -> list:
    return [
        _stance_entry(s)
        for s in intel_store.stance_history(
            limit=min(max(limit, 1), 500), offset=max(offset, 0)
        )
    ]


@router.get("/signals/macro")
async def signals_macro(limit: int = 50) -> dict:
    active = macro_store.get_active_events(
        min_severity=bot_config.MACRO_MIN_SEVERITY_INJECT
    )
    recent = macro_store.list_events(limit=min(max(limit, 1), 200))
    def _summary(e: dict) -> dict:
        return {
            "id": e.get("id"),
            "title": e.get("title"),
            "url": e.get("url"),
            "source": e.get("source"),
            "ingested_at": e.get("ingested_at"),
            "published_at": e.get("published_at"),
            "severity": e.get("severity"),
            "eth_bias": e.get("eth_bias"),
            "category": e.get("category"),
            "eth_impact_summary": e.get("eth_impact_summary"),
            "posture_hints": e.get("posture_hints") or [],
            "expires_at": e.get("expires_at"),
            "status": e.get("status"),
        }

    return {
        "active": [_summary(e) for e in active],
        "recent": [_summary(e) for e in recent],
    }


@router.get("/signals/zmove")
async def signals_zmove(limit: int = 50) -> dict:
    return {
        "threshold": bot_config.ZMOVE_THRESHOLD,
        "lookback_h": bot_config.ZMOVE_LOOKBACK_H,
        "events": intel_store.recent_zmove_events(limit=min(max(limit, 1), 200)),
    }


@router.get("/signals/funding")
async def signals_funding() -> dict:
    return {
        "enabled": bot_config.FUNDING_ENABLED,
        "persist_periods": bot_config.FUNDING_PERSIST_PERIODS,
        "switch_confirm_periods": bot_config.FUNDING_SWITCH_CONFIRM_PERIODS,
        "products": {
            pid: _funding_payload(pid) for pid in bot_config.FUNDING_PRODUCTS
        },
    }


@router.get("/ideas/hq")
async def ideas_hq(limit: int = 25) -> list:
    rows = ledger.get_latest(min(max(limit, 1), 100) * 3)
    ideas = [r for r in rows if str(r.get("action")) != "no_trade"]
    out = []
    for r in ideas[: min(max(limit, 1), 100)]:
        out.append(
            {
                "cycle_id": r.get("cycle_id"),
                "ts": r.get("ts"),
                "product_id": r.get("product_id"),
                "action": r.get("action"),
                "entry": r.get("entry"),
                "stop_loss": r.get("stop_loss"),
                "take_profits": r.get("take_profits"),
                "risk_reward": r.get("risk_reward"),
                "rationale": r.get("rationale"),
                "setup_tags": r.get("setup_tags"),
                "executed": r.get("executed"),
            }
        )
    return out


@router.get("/subscribers")
async def subscribers() -> dict:
    """Broadcast recipients for the public volume lane (trade_ideas mill).

    The mill sends its cards through this bot's token, so it needs the same
    recipient list the agent uses — resolved here so paywall/allowlist logic
    lives in exactly one place.
    """
    recipients = access.broadcast_recipient_ids()
    return {
        "paywall_enabled": config.PAYWALL_ENABLED,
        "count": len(recipients),
        "recipients": recipients,
    }


@router.get("/charts/cycle")
async def charts_cycle() -> FileResponse:
    thesis = intel_store.latest_long_thesis()
    path = (thesis or {}).get("chart_path")
    if not path or not Path(str(path)).exists():
        raise HTTPException(status_code=404, detail="No cycle chart")
    resolved = Path(str(path)).resolve()
    charts_root = Path(config.CHARTS_DIR).resolve()
    if charts_root not in resolved.parents and resolved != charts_root:
        raise HTTPException(status_code=404, detail="No cycle chart")
    return FileResponse(str(resolved), media_type="image/png")
